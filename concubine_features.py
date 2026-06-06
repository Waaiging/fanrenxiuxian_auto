"""
【侍妾功能模块 —— 所有账号脚本共享】

提供 ConcubineMixin 混入类，封装所有与侍妾（道侣）相关的操作：
  1. 入梦寻图 —— 虚天残图/苍坤残图线索收集
  2. 共历心劫 —— 三轮抉择（稳/狠/骗），侍妾连携
  3. 天机代卜 —— 侍妾代卜避劫，提升闭关收益

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。
"""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta

from log_utils import (
    command_send_allowed,          # 指令频率守卫
    log_incoming_message,          # 记录收到的消息
    notify_unrecognized_response,  # 无法识别的回复告警
    record_bot_no_response,        # 记录机器人无响应
    record_bot_response,           # 记录机器人有响应
    remember_script_send_intent,   # 记录脚本即将发送
    remember_script_sent_message,  # 记录脚本已发送
    schedule_command_auto_delete,  # 安排自动删除
    wait_for_bot_activity_before_send,  # 等待机器人活跃
)


log = logging.getLogger("Concubine")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONCUBINE_GRACE_SECONDS = 60  # 冷却宽容时间，避免频繁请求

# =====================================================================
# 侍妾神通配置
# 每个任务包含：显示名称、指令、状态键、冷却时间(秒)、状态标签
# =====================================================================
CONCUBINE_TASKS = {
    "dream": {
        "label": "入梦寻图",
        "command": ".入梦寻图",
        "state_key": "next_dream_map_time",
        "cooldown": 480 * 60,             # 8小时
        "status_label": "入梦寻图冷却",
    },
    "heart_trial": {
        "label": "共历心劫",
        "command": ".共历心劫",
        "state_key": "next_heart_trial_time",
        "cooldown": 600 * 60,             # 10小时
        "status_label": "共历心劫冷却",
    },
    "divination": {
        "label": "天机代卜",
        "command": ".天机代卜",
        "state_key": "next_divination_time",
        "cooldown": 720 * 60,             # 12小时
        "status_label": "天机代卜冷却",
    },
}


# =====================================================================
# 时间工具函数（与主脚本保持一致）
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

def is_future(value):
    try:
        return str_to_dt(value) > datetime.now()
    except Exception:
        return False

def add_seconds_str(value, seconds):
    return dt_to_str(str_to_dt(value) + timedelta(seconds=seconds))

def seconds_until(value):
    try:
        return max(0, (str_to_dt(value) - datetime.now()).total_seconds())
    except Exception:
        return 0

def parse_duration_seconds(text):
    """解析游戏回复中的时间文本（如"13分钟26秒"）为秒数"""
    if not text:
        return -1
    clean = text.replace("**", "").replace(" ", "")
    h = re.search(r"(\d+)(?:小时|h)", clean)
    m = re.search(r"(\d+)(?:分钟|分|m)", clean)
    s = re.search(r"(\d+)(?:秒|s)", clean)
    total = 0
    found = False
    if h:
        total += int(h.group(1)) * 3600
        found = True
    if m:
        total += int(m.group(1)) * 60
        found = True
    if s:
        total += int(s.group(1))
        found = True
    return total if found else -1


# =====================================================================
# 默认状态数据
# =====================================================================

def concubine_default_state():
    """返回侍妾功能的默认状态字典"""
    return {
        "last_concubine_status_time": "",
        "next_dream_map_time": "",
        "next_heart_trial_time": "",
        "next_divination_time": "",
    }


# =====================================================================
# ConcubineMixin 混入类
# 包含所有侍妾相关的方法，通过多重继承混入主 Cultivator 类
# =====================================================================

class ConcubineMixin:
    """侍妾功能混入类，提供侍妾神通的所有操作。"""

    # ---- 状态管理 ----

    def ensure_concubine_state(self):
        """确保状态字典包含所有侍妾默认键"""
        changed = False
        for key, value in concubine_default_state().items():
            if key not in self.state:
                self.state[key] = value
                changed = True
        if changed:
            self.save_state()

    def has_concubine_status_cache(self):
        """是否已有侍妾状态缓存"""
        return bool(self.state.get("last_concubine_status_time"))

    def parse_concubine_status(self, text):
        """
        解析.我的侍妾回复，提取各神通的冷却时间。
        冷却已过的任务标记为就绪（state_key 置空），
        有冷却的标记为等待时间。
        """
        if not text:
            return False
        updated = False
        clean = text.replace("**", "")
        for task in CONCUBINE_TASKS.values():
            line_match = re.search(rf"{re.escape(task['status_label'])}\s*[：:]\s*([^\n]+)", clean)
            if not line_match:
                continue
            value = line_match.group(1).strip()
            if any(k in value for k in ["无", "可用", "可施展", "已就绪"]):
                self.state[task["state_key"]] = ""
                updated = True
                continue
            cd = parse_duration_seconds(value)
            if cd == 0:
                self.state[task["state_key"]] = ""
                updated = True
                continue
            if cd > 0:
                self.state[task["state_key"]] = add_seconds_str(now_str(), cd + CONCUBINE_GRACE_SECONDS)
                updated = True
        if updated:
            self.state["last_concubine_status_time"] = now_str()
            self.save_state()
        return updated

    def record_concubine_cd(self, task_key, response_text=""):
        """
        记录侍妾任务的冷却时间。
        优先从回复文本中解析具体冷却时间，解析失败则使用默认冷却。
        """
        task = CONCUBINE_TASKS[task_key]
        cd = -1
        if response_text and any(k in response_text for k in ["冷却", "后再", "尚未"]):
            for line in response_text.replace("**", "").splitlines():
                if task["label"] in line or "冷却" in line or "后再" in line:
                    cd = parse_duration_seconds(line)
                    if cd > 0:
                        break
        if cd <= 0:
            cd = task["cooldown"]
        self.state[task["state_key"]] = add_seconds_str(now_str(), cd + CONCUBINE_GRACE_SECONDS)
        self.state["last_concubine_status_time"] = now_str()
        self.save_state()
        log.info(f"Concubine {task['label']}: next at {self.state[task['state_key']]}")

    def is_known_concubine_response(self, task_key, text):
        """判断侍妾回复是否可识别（含已知关键词）"""
        if not text:
            return False
        task = CONCUBINE_TASKS[task_key]
        clean = text.replace("**", "")
        known_keywords = [
            task["label"], "冷却", "后再", "尚未",
            "成功", "获得", "完成", "机缘",
            "残图", "入梦", "寻图", "天机", "代卜", "卜算",
            "共历心劫", "心劫", "道侣", "护道",
        ]
        return any(k in clean for k in known_keywords)

    def defer_concubine_task(self, task_key, seconds=600):
        """推迟侍妾任务（遇到异常时使用）"""
        task = CONCUBINE_TASKS[task_key]
        self.state[task["state_key"]] = add_seconds_str(now_str(), seconds)
        self.save_state()

    # ---- 状态同步 ----

    async def sync_concubine_status_if_needed(self):
        """如需刷新侍妾状态缓存，发送.我的侍妾并解析"""
        self.ensure_concubine_state()
        if self.has_concubine_status_cache():
            return
        log.info("Concubine: cache missing, querying .我的侍妾")
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = (status_msg.text or "") if status_msg else ""
        if status_msg and hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id
        if not self.parse_concubine_status(status_text):
            log.warning("Concubine: .我的侍妾 response did not contain cooldowns.")
            if status_text:
                notify_unrecognized_response(self, ".我的侍妾", status_text, log, "侍妾状态")

    async def refresh_heart_trial_cooldown_after_uncertain(self, context):
        """不确定状态下重新查询侍妾状态，确认共历心劫的冷却"""
        task = CONCUBINE_TASKS["heart_trial"]
        log.info(f"Concubine 共历心劫: uncertain {context}, querying .我的侍妾 before alert.")
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = (status_msg.text or "") if status_msg else ""
        if status_msg and hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id
        if not status_text:
            log.warning(f"Concubine 共历心劫: no .我的侍妾 response while checking {context}.")
            return False
        if not self.parse_concubine_status(status_text):
            log.warning(f"Concubine 共历心劫: .我的侍妾 did not expose cooldown while checking {context}.")
            return False
        next_time = self.state.get(task["state_key"], "")
        if next_time and is_future(next_time):
            log.info(f"Concubine 共历心劫: cooldown detected after uncertain {context}, next at {next_time}.")
            return True
        log.warning(f"Concubine 共历心劫: still no cooldown after uncertain {context}.")
        return False

    # ---- 执行神通 ----

    async def execute_concubine_direct(self, task_key):
        """执行非心劫类侍妾神通（入梦寻图/天机代卜），直接发送指令"""
        task = CONCUBINE_TASKS[task_key]
        log.info(f"Concubine: sending {task['command']}")
        # 发送前确保身份对齐（防止化身协程在间隙抢走身份）
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        resp = await self.send_and_wait_feedback(task["command"], timeout=90, delete_after=False)
        if resp:
            if not self.is_known_concubine_response(task_key, resp):
                log.warning(f"Concubine {task['label']}: unrecognized response, skipping this run.")
                notify_unrecognized_response(self, task["command"], resp, log, task["label"])
                self.defer_concubine_task(task_key)
                return
            self.record_concubine_cd(task_key, resp)
            # 入梦寻图：集齐残纹后自动发送 .拼图
            if task_key == "dream" and resp and "4/4" in resp:
                log.info("Concubine dream: progress 4/4 reached, sending .拼图")
                await asyncio.sleep(3)
                puzzle_resp = await self.send_and_wait_feedback(".拼图", timeout=90, delete_after=False)
                if puzzle_resp:
                    log.info(f"Concubine .拼图 response: {puzzle_resp[:120]}")
                else:
                    log.warning("Concubine .拼图: no response.")
        else:
            log.warning(f"Concubine {task['label']}: no response, retrying later.")
            self.defer_concubine_task(task_key)

    # ---- 消息编辑检测 ----

    async def get_message_after_delay(self, msg, delay_sec=12):
        """延迟后重新获取消息（等待游戏机器人编辑回复）"""
        if not msg:
            return None
        await asyncio.sleep(delay_sec)
        try:
            return await self.client.get_messages(self.target_chat_id, ids=msg.id)
        except Exception as e:
            log.warning(f"Concubine: failed to fetch edited message {msg.id}: {e}")
            return msg

    async def wait_for_message_edit(self, msg, timeout_sec=45, poll_sec=3):
        """
        轮询等待消息被游戏机器人编辑。
        游戏机器人有时先发后编辑（逐步揭示结果），此函数用于等待最终版本。
        """
        if not msg:
            return None
        before_text = msg.text or ""
        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        latest = msg
        while datetime.now() < deadline:
            await asyncio.sleep(poll_sec)
            try:
                fetched = await self.client.get_messages(self.target_chat_id, ids=msg.id)
            except Exception as e:
                log.warning(f"Concubine: failed to poll edited message {msg.id}: {e}")
                return latest
            if fetched:
                latest = fetched
                after_text = fetched.text or ""
                if after_text != before_text:
                    record_bot_response(self)
                    await asyncio.sleep(2)
                    return fetched
        log.warning(f"Concubine: message {msg.id} was not edited within {timeout_sec}s.")
        record_bot_no_response(self, ".稳", log)
        return latest

    # ---- 共历心劫核心流程 ----

    async def wait_for_heart_trial_round_result(self, current_msg, sent_msg, idx, timeout_sec=90, poll_sec=3):
        """
        等待心劫一轮的结果。
        同时监听：
        1. feedback_events（回复消息触发）
        2. 消息编辑（游戏机器人编辑原消息）
        任一方式确认本轮完成即返回。
        """
        if not current_msg:
            return None, "", False
        before_text = current_msg.text or ""
        latest_msg = current_msg
        latest_text = before_text
        sent_id = getattr(sent_msg, "id", None)
        evt = asyncio.Event()
        if sent_id is not None:
            self.feedback_events[sent_id] = evt
            self.feedback_commands[sent_id] = ".稳"
            self.feedback_sent_ts[sent_id] = time.monotonic()
            self.feedback_senders = getattr(self, "feedback_senders", {})
            self.feedback_senders[sent_id] = getattr(sent_msg, "sender_id", None)
            self.feedback_identities = getattr(self, "feedback_identities", {})
            self.feedback_identities[sent_id] = getattr(self, "current_identity", "主魂")

        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        try:
            while datetime.now() < deadline:
                remaining = max(0.1, (deadline - datetime.now()).total_seconds())
                try:
                    await asyncio.wait_for(evt.wait(), timeout=min(poll_sec, remaining))
                except asyncio.TimeoutError:
                    pass

                if sent_id is not None and evt.is_set():
                    reply_text = self.last_feedback_text.pop(sent_id, "").strip()
                    reply_msg = self.last_feedback_msg.pop(sent_id, None)
                    evt.clear()
                    if reply_text:
                        record_bot_response(self)
                        latest_msg = reply_msg or latest_msg
                        latest_text = reply_text
                        if self.heart_trial_round_confirmed(reply_text, idx):
                            return latest_msg, latest_text, True
                        if any(k in reply_text for k in ["冷却", "尚未", "后再", "失败", "无法", "错误", "锚点已散"]):
                            return latest_msg, latest_text, False

                try:
                    fetched = await self.client.get_messages(self.target_chat_id, ids=current_msg.id)
                except Exception as e:
                    log.warning(f"Concubine: failed to poll heart trial message {current_msg.id}: {e}")
                    break

                if fetched and (fetched.text or "") != before_text:
                    latest_msg = fetched
                    latest_text = fetched.text or ""
                    record_bot_response(self)
                    if self.heart_trial_round_confirmed(latest_text, idx):
                        await asyncio.sleep(2)
                        return latest_msg, latest_text, True
                    if any(k in latest_text for k in ["冷却", "尚未", "后再", "失败", "无法", "错误", "锚点已散"]):
                        return latest_msg, latest_text, False

            log.warning(f"Concubine: .稳 ({idx}/3) did not produce a confirmed round within {timeout_sec}s.")
            record_bot_no_response(self, ".稳", log)
            return latest_msg, latest_text, False
        finally:
            if sent_id is not None:
                self.feedback_events.pop(sent_id, None)
                self.last_feedback_text.pop(sent_id, None)
                self.last_feedback_msg.pop(sent_id, None)
                self.feedback_commands.pop(sent_id, None)
                self.feedback_sent_ts.pop(sent_id, None)
                self.feedback_senders.pop(sent_id, None)
                if hasattr(self, "feedback_identities"):
                    self.feedback_identities.pop(sent_id, None)

    async def delete_heart_trial_command_later(self, sent_msg, delay_sec=120):
        """延迟删除心劫指令（清理群聊记录）"""
        if not sent_msg or not hasattr(self, "delete_msg"):
            return
        await asyncio.sleep(delay_sec)
        try:
            await self.delete_msg(sent_msg)
        except Exception as e:
            log.warning(f"Concubine: delayed .稳 delete failed: {e}")

    def heart_trial_round_confirmed(self, text, idx):
        """检查心劫第idx轮是否已确认完成"""
        clean = (text or "").replace("**", "")
        if self.heart_trial_settled(text):
            return True
        current_labels = self.heart_trial_round_labels(idx)
        next_labels = self.heart_trial_round_labels(idx + 1)
        current_done = any(f"第{label}轮已定" in clean for label in current_labels)
        next_prompt = any(f"第{label}轮" in clean for label in next_labels)
        return current_done and next_prompt

    def heart_trial_settled(self, text):
        """检查心劫是否已结算（全部三轮完成）"""
        clean = (text or "").replace("**", "")
        return "坠魔心劫·结算" in clean

    @staticmethod
    def heart_trial_round_labels(idx):
        """心劫第n轮的数字标签（中英文）"""
        labels = {1: ("1", "一"), 2: ("2", "二"), 3: ("3", "三")}
        return labels.get(idx, (str(idx),))

    def heart_trial_round_prompt(self, text, idx):
        """检查文本是否包含第idx轮的提示"""
        clean = (text or "").replace("**", "")
        if "坠魔心劫" not in clean:
            return False
        return any(f"第{label}轮" in clean for label in self.heart_trial_round_labels(idx))

    def heart_trial_requires_reply_target(self, text):
        """检查游戏机器人是否要求回复到指定的侍妾消息"""
        clean = (text or "").replace("**", "")
        return (
            "请回复" in clean
            and ".共历心劫" in clean
            and ("侍妾" in clean or "道侣" in clean)
        )

    async def execute_heart_trial(self):
        """
        共历心劫完整流程。

        步骤：
        1. 查询侍妾状态（.我的侍妾）
        2. 发送.共历心劫
        3. 如果游戏机器人要求回复到侍妾消息，尝试3次
        4. 依次发送三轮.稳 (稳/稳/稳 策略)
        5. 每轮等待结果确认
        6. 结算后记录冷却
        """
        task = CONCUBINE_TASKS["heart_trial"]
        log.info("Concubine: starting .共历心劫 flow via .我的侍妾")
        # 每条命令前确保身份对齐（防止化身协程在间隙抢走身份）
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        if not status_msg:
            log.warning("Concubine 共历心劫: missing .我的侍妾 status message.")
            self.defer_concubine_task("heart_trial")
            return

        if hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id

        status_text = status_msg.text or ""
        if not self.parse_concubine_status(status_text) and status_text:
            notify_unrecognized_response(self, ".我的侍妾", status_text, log, "共历心劫前状态")
            self.defer_concubine_task("heart_trial")
            return
        if self.state.get(task["state_key"]) and is_future(self.state[task["state_key"]]):
            log.info(f"Concubine 共历心劫: still on CD until {self.state[task['state_key']]}")
            return

        trial_msg = None
        trial_text = ""
        for attempt in range(1, 4):
            log.info(f"Concubine 共历心劫: replying .共历心劫 to .我的侍妾 status message ({attempt}/3).")
            # 每次发送前确保身份对齐
            if hasattr(self, 'switch_back_to_main'):
                await self.switch_back_to_main()
            trial_msg = await self.send_and_wait_feedback(
                task["command"], reply_to=status_msg.id,
                timeout=90, return_response_msg=True, delete_after=False,
            )
            trial_text = (trial_msg.text or "") if trial_msg else ""
            if not self.heart_trial_requires_reply_target(trial_text):
                break
            log.warning("Concubine 共历心劫: bot did not accept the reply target; retrying.")
            if attempt < 3:
                status_msg = await self.send_and_wait_feedback(
                    ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
                )
                if not status_msg:
                    break
                await asyncio.sleep(3)

        if not trial_msg:
            log.warning("Concubine 共历心劫: missing .共历心劫 response message.")
            self.defer_concubine_task("heart_trial")
            return

        if any(k in trial_text for k in ["冷却", "后再", "尚未"]):
            self.record_concubine_cd("heart_trial", trial_text)
            return
        if self.heart_trial_requires_reply_target(trial_text):
            log.warning("Concubine 共历心劫: bot still requires reply target.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫回复目标")
            self.defer_concubine_task("heart_trial", 600)
            return
        if not self.heart_trial_round_prompt(trial_text, 1):
            log.warning("Concubine 共历心劫: response did not start round 1.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫开局")
            self.defer_concubine_task("heart_trial", 600)
            return
        if not self.is_known_concubine_response("heart_trial", trial_text):
            log.warning("Concubine 共历心劫: unrecognized trial response.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫")
            self.defer_concubine_task("heart_trial")
            return

        # 三轮心劫循环（每轮发.稳）
        current_msg = trial_msg
        # 发 .稳 前确保身份对齐
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        async with self.cmd_lock:
            for idx in range(1, 4):
                confirmed = False
                current_text = ""
                for attempt in range(1, 4):
                    try:
                        pause_event = getattr(self, "pause_event", None)
                        if pause_event is not None:
                            await pause_event.wait()
                        if not await wait_for_bot_activity_before_send(self, ".稳", log):
                            return
                        if not command_send_allowed(self, ".稳", log):
                            return
                        remember_script_send_intent(self, ".稳")
                        sent = await self.client.send_message(self.target_chat_id, ".稳", reply_to=current_msg.id)
                        remember_script_sent_message(self, sent)
                        schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                        _identity = getattr(self, "current_identity", None)
                        _tag = f" [{_identity}]" if _identity else ""
                        log.info(f"🟢 OUT{_tag}:\n.稳 ({idx}/3, try {attempt}/3)")
                    except Exception as e:
                        log.error(f"Concubine 共历心劫: failed to send .稳 ({idx}/3): {e}")
                        return

                    result_msg, current_text, confirmed = await self.wait_for_heart_trial_round_result(
                        current_msg, sent, idx, timeout_sec=90, poll_sec=3,
                    )
                    if result_msg:
                        current_msg = result_msg
                        await log_incoming_message(self, f".稳 {idx}/3 try {attempt}/3", current_text, msg=result_msg, logger=log)
                        if self.heart_trial_settled(current_text):
                            self.record_concubine_cd("heart_trial")
                            return
                        if confirmed:
                            break
                        if self.heart_trial_round_prompt(current_text, idx):
                            if attempt < 3:
                                log.warning(f"Concubine 共历心劫: still on round {idx}; retrying.")
                                await asyncio.sleep(3)
                                continue
                            if await self.refresh_heart_trial_cooldown_after_uncertain(f".稳 第{idx}轮"):
                                return
                        log.warning(f"Concubine 共历心劫: .稳 ({idx}/3) did not confirm.")
                        notify_unrecognized_response(self, ".稳", current_text, log, f"共历心劫第{idx}轮")
                        self.defer_concubine_task("heart_trial", 600)
                        return
                    log.warning(f"Concubine 共历心劫: missing edit after .稳 ({idx}/3, try {attempt}/3).")
                    if attempt < 3:
                        await asyncio.sleep(3)
                        continue
                    if await self.refresh_heart_trial_cooldown_after_uncertain(f".稳 第{idx}轮无编辑"):
                        return
                    self.defer_concubine_task("heart_trial", 600)
                    return

                if not confirmed:
                    return

        self.record_concubine_cd("heart_trial")

    # ---- 主循环 ----

    async def run_concubine_loop(self):
        """
        侍妾功能主循环。
        按优先级依次执行：入梦寻图 → 共历心劫 → 天机代卜
        全部有冷却时等待最短的冷却时间。
        """
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(20, 60))
        await self.sync_concubine_status_if_needed()

        while self.is_running:
            self.ensure_concubine_state()
            # 主循环只检查冷却，不主动切回主魂；实际发送时再对齐身份。
            if hasattr(self, '_wait_for_main_identity'):
                await self._wait_for_main_identity()
            try:
                executed = False
                for task_key in ("dream", "heart_trial", "divination"):
                    task = CONCUBINE_TASKS[task_key]
                    next_time = self.state.get(task["state_key"], "")
                    if next_time and is_future(next_time):
                        continue
                    if task_key == "heart_trial":
                        await self.execute_heart_trial()
                    else:
                        await self.execute_concubine_direct(task_key)
                    executed = True
                    await asyncio.sleep(random.randint(8, 18))
                    break

                if executed:
                    continue

                waits = [
                    seconds_until(self.state.get(task["state_key"], ""))
                    for task in CONCUBINE_TASKS.values()
                    if self.state.get(task["state_key"], "") and is_future(self.state[task["state_key"]])
                ]
                sleep_for = max(60, min(waits) + random.randint(15, 45)) if waits else 300
                log.info(f"Concubine loop complete. Sleep {int(sleep_for)}s.")
                await asyncio.sleep(sleep_for)
            except Exception as e:
                log.error(f"Concubine loop error: {e}")
                await asyncio.sleep(300)
