#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Intelligent Cultivator v8.5 (LingXiaoGong Edition)
- Specialized for LingXiaoGong Cloud Stairs and Cultivation.
- STRICT feedback matching: Only accepts Mentions or Reply-To.
- Synchronized Actions: Uses Lock to prevent command racing between loops.
- 300s retention, 10-30s variance.

【模块说明】
本脚本是「凡人修仙传」Telegram 游戏的主号自动修仙脚本（主控脚本）。
负责自动完成以下核心玩法循环：
  1. 登天阶（凌霄云阶）—— 爬塔核心玩法
  2. 深度闭关 —— 自动开闭关、8小时等待、结算重开
  3. 引九天罡风 —— 增益 buff 循环
  4. 问心台 —— 爬塔辅助 buff
  5. 每日任务 —— 闯塔、宗门点卯
  6. 元婴出窍 —— 元婴期能力循环
  7. 探寻裂缝 —— 定时搜寻裂缝
  8. 抚摸法宝 —— 本命法宝器灵互动
  9. 侍妾神通 —— 侍妾相关操作
  10. BOSS 警报 —— 监测关键词并发送通知
脚本运行在 WSL 中，通过 Telethon 连接 Telegram，与游戏机器人交互。
"""

import asyncio          # 异步 I/O 框架，所有游戏交互都是异步的
import time             # 时间戳、sleep 等基本时间操作
import json
STAR_GAZING_INTERVAL_HOURS = 3
STAR_GAZING_MONITOR_LEAD_SECONDS = 3 * 60
STAR_GAZING_COMMAND_LEAD_SECONDS = 60
STAR_GAZING_SHIFT_PROFILE = "early"
STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS = (-3, 2)
STAR_GAZING_SHIFT_LEAD_SECONDS = -STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS[1]
STAR_GAZING_SHIFT_GRACE_SECONDS = 1
STAR_GAZING_SHIFT_REPEAT_COUNT = 1
STAR_GAZING_SHIFT_REPEAT_INTERVAL_SECONDS = 3
STAR_GAZING_DAILY_FALLBACK_HOUR = 23
STAR_GAZING_DAILY_FALLBACK_MINUTE = 59
STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
STAR_GAZING_ACTIVE_WINDOW_SECONDS = 59
STAR_GAZING_ROTATING_AVATARS = ["素缘子"]
STAR_GAZING_SHIFT_TARGET = "@Weeguu"
STAR_ATTRACTION_TARGET = "天雷星"
STAR_ATTRACTION_COMMAND = f".牵引星辰 {STAR_ATTRACTION_TARGET}"
STAR_ATTRACTION_COOLDOWN_SECONDS = 36 * 3600
STAR_PRE_APPEASE_LEAD_SECONDS = 60
STAR_STATUS_RETRY_SECONDS = 10 * 60
STAR_INSUFFICIENT_RETRY_SECONDS = 60 * 60
STAR_ATTRACTION_AVATARS = {"素缘子"}
FORMATION_TARGET_INITIATORS = {
    "crayonxxin": "副号-厚土",
    "lvdoumiao": "副号-缘生子",
    "ding303": "副号-寻真子",
}
FORMATION_ASSIST_AVATARS = ["素缘子"]

STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
             # 读写 JSON 配置文件/状态文件
import os               # 文件路径、环境变量操作
import sys              # 标准输入输出
import re               # 正则表达式，用于解析游戏回复中的时间、进度等信息
import random           # 随机数，用于在等待时间中加入随机抖动，模拟人类行为
import logging          # 日志系统
# 强制设置时区为北京时间，解决 Python 与系统时间偏差
# Python 默认使用 UTC，而游戏机器人按北京时间运行，必须对齐
os.environ['TZ'] = 'Asia/Shanghai'
if hasattr(time, 'tzset'):
    time.tzset()        # 让 C 库重新读取时区配置（Unix 系统专用）
from datetime import datetime, timedelta  # 日期时间处理


def star_gazing_shift_dt(target_dt, now=None, fate_type="", logger=None):
    """Return this account's layered shift send time."""
    return predicted_star_shift_dt(
        target_dt,
        now=now,
        fate_type=fate_type,
        logger=logger,
        shift_profile=STAR_GAZING_SHIFT_PROFILE,
    )

from telethon import TelegramClient, events  # Telegram 客户端框架，消息事件

# 导入各个功能模块（分离到不同文件中以降低本文件复杂度）
from auto_reply_features import is_auto_reply_followup, maybe_auto_reply_exchange
from common_command_features import CommonCommandMixin, common_command_default_state
from command_feedback import _handle_telegram_send_protection, send_and_wait_feedback_common
from concubine_features import ConcubineMixin, concubine_default_state
from fishing_features import FishingMixin
from star_gazing_collector import predicted_star_shift_dt, record_star_gazing_event
from yinluo_features import YinluoMixin, YINLUO_IDENTITY
from log_utils import (
    CommandLogFilter,          # 日志过滤器，过滤掉指令内容（保护隐私）
    cap_command_retries,       # 限制指令重试次数
    clear_command_guard_block, # 清理响应语义设置的命令保护
    command_send_allowed,      # 检查是否允许发送指令
    command_send_precheck,     # 不记录发送次数的切换前预检
    force_command_guard_block, # 按业务响应设置命令保护
    handle_clear_history_command, # 处理清屏指令
    handle_anti_bot_challenge, # 处理反机器人验证
    is_deep_meditation_ongoing_response,    # 判断是否为"正在深度闭关"的回复
    is_deep_meditation_settlement_response, # 判断是否为"闭关结算"的回复
    is_game_bot_sender,        # 判断消息发送者是否为游戏机器人
    is_not_deep_meditation_response,        # 判断是否为"未在闭关"的回复
    is_yuanying_out_settlement_response,    # 判断元婴/元神归窍结算
    log_edited_message_if_needed,           # 记录编辑过的消息
    log_incoming_message,      # 记录收到的消息
    is_reply_to_manual_command,              # 检查是否为手动指令回复
    log_manual_outgoing_if_needed,          # 记录手动发送的消息
    log_mention_if_needed,     # 记录 @提及
    match_pending_edited_feedback,          # 安全匹配编辑后的机器人反馈
    match_pending_feedback_by_message_id,   # 按消息 ID 匹配编辑反馈
    match_pending_feedback_by_reply,        # 按 reply_to 匹配机器人反馈
    notify_unrecognized_response,           # 通知无法识别的回复
    periodic_log_prune,        # 定期修剪日志文件
    prune_log_file,            # 修剪日志文件
    record_bot_no_response,    # 记录机器人无响应
    record_bot_response,       # 记录机器人响应
    record_cultivation_profile_from_text, # 同步境界/修为资料
    record_command_sent,      # 指令台账
    record_game_bot_activity,  # 记录游戏机器人活动
    record_message_event,      # 消息事件库
    record_manual_command_reply_state_if_needed, # 同步手动指令回复状态
    recent_profile_identity_for_text, # 识别无 reply 档案回复的身份
    remember_script_send_intent,  # 记录脚本发送意图
    remember_script_sent_message, # 记录脚本已发送的消息
    schedule_command_auto_delete, # 安排命令自动删除
    sender_is_pause_admin,     # 判断暂停/恢复控制消息是否来自授权发送者
    send_text_alert,           # 发送文本告警
    is_edited_message_for_current_account, # 判定消息是否针对当前账号的编辑
    feedback_response_conflicts, # 判定回复文本是否属于其他指令家族
    feedback_response_matches_command, # 判定回复文本是否正向匹配该指令
    feedback_response_requires_positive_match, # 已知指令需要正向内容匹配
    is_reply_to_untracked_message, # 带 reply_to 但不属于本脚本指令的回复
    maybe_handle_han_soul_choice,   # 韩天尊神魂抉择自动回复
    wait_for_bot_activity_before_send,  # 发送前等待机器人活动确认
    mentions_self,             # 判定消息是否提到了当前账号
    mentions_other_user,        # 判定消息是否明确提到了其他账号
    mentions_other_user_for_identity, # 身份感知的“其他用户”提及判定
    tracked_command_identity_for_reply, # 识别手动/脚本指令回复对应身份
    text_targets_current_account,       # 判定机器人文本是否明确指向当前账号
)

# =====================================================================
# 配置文件路径（硬编码，与脚本同目录）
# =====================================================================
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')    # 主配置文件
LOG_FILE = os.path.join(CONFIG_DIR, 'cultivator.log')    # 日志文件
STATE_FILE = os.path.join(CONFIG_DIR, 'state_main.json') # 主号状态持久化文件

# =====================================================================
# 时间格式与冷却时间常量
# =====================================================================
# 标准时间格式，用于所有 state 中的时间字符串
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_SCHEDULER_SLEEP_SECONDS = 300
SWITCH_COMMAND_LEAD_SECONDS = 3

# 各项活动的冷却时间（秒），这些值来自游戏设定
CLOUD_STAIRS_CD_SECONDS = 3 * 3600          # 登天阶冷却：3 小时
HEART_PLATFORM_MIN_INTERVAL_SECONDS = 3 * 3600  # 问心台最小间隔：3 小时
NINE_HEAVEN_WIND_CD_SECONDS = 12 * 3600     # 九天罡风冷却：12 小时
DAILY_TASK_START_HOUR = 7                   # 每日任务开始小时（北京时间早上 7 点）
DAILY_TASK_START_MINUTE = 0                 # 每日任务开始分钟
DESTINY_AVATAR = "无咎子"                  # 观命/定命专属化身
DESTINY_WINDOW_START_HOUR = 0               # 观命开始小时
DESTINY_WINDOW_START_MINUTE = 10            # 观命开始分钟
DESTINY_WINDOW_END_HOUR = 0                 # 观命结束小时
DESTINY_WINDOW_END_MINUTE = 20              # 观命结束分钟
DESTINY_MEDITATION_DEFER_SECONDS = 20 * 60  # 观命窗口前短暂暂缓新开闭关
DESTINY_OBSERVE_FAILURE_KEYWORDS = (
    "无法", "不能", "不可", "闭关中", "深度闭关", "正在闭关", "闭关状态",
    "冷却", "修为不足", "并非", "未开启", "错误"
)
SECT_SKILL_MAX_DAILY = 3                    # 宗门传功每日上限次数
YUANYING_OUT_CD_SECONDS = 8 * 3600          # 元婴出窍冷却：8 小时
RIFT_SEARCH_CD_SECONDS = 12 * 3600          # 探寻裂缝冷却：12 小时
TREASURE_TOUCH_COMMAND = ".抚摸法宝 玄天斩灵剑"  # 抚摸法宝的具体指令
NURTURE_SPIRIT_COMMAND = ".温养器灵 斩灵"  # 温养器灵指令
TREASURE_TOUCH_CD_SECONDS = 2 * 3600        # 抚摸法宝冷却：2 小时
SPIRIT_TREE_AVATAR = "缘生子"
SPIRIT_TREE_IRRIGATION_COMMAND = ".灵树灌溉"
SPIRIT_TREE_STATUS_COMMAND = ".灵树状态"
SPIRIT_TREE_HARVEST_COMMAND = ".采摘灵果"
SPIRIT_TREE_GUARD_COMMAND = ".协同守山"
SPIRIT_TREE_IRRIGATION_STATUS = "灌溉期"
SPIRIT_TREE_MATURE_STATUS = "成熟采摘期"
SPIRIT_TREE_MATURE_SECONDS = 24 * 3600
SPIRIT_TREE_HARVEST_LOCK_SECONDS = 48 * 3600
SPIRIT_TREE_MATURE_KEYWORDS = ("灵果已完全成熟", "采摘期开启", "成熟采摘期")
SPIRIT_TREE_GUARD_CD_SECONDS = 5 * 3600
SPIRIT_TREE_GUARD_SUCCESS_RETRY_SECONDS = 5 * 60
SPIRIT_TREE_GUARD_ERROR_BLOCK_SECONDS = 60 * 60


def spirit_tree_default_state():
    return {
        "next_spirit_tree_irrigation_time": "",
        "spirit_tree_status": SPIRIT_TREE_IRRIGATION_STATUS,
        "spirit_tree_mature_until": "",
        "spirit_tree_harvested_in_mature_period": False,
        "spirit_tree_harvest_attempted_in_mature_period": False,
        "spirit_tree_harvest_pending": False,
        "spirit_tree_last_harvest_time": "",
        "spirit_tree_last_harvest_attempt_time": "",
        "next_spirit_tree_harvest_time": "",
        "spirit_tree_irrigation_times": {},
        "spirit_tree_last_mature_detected_time": "",
        "spirit_tree_last_mature_msg_id": 0,
        "spirit_tree_last_status_time": "",
        "spirit_tree_invasion_status": "",
        "spirit_tree_guard_pending": False,
        "next_spirit_tree_guard_time": "",
        "last_spirit_tree_guard_time": "",
        "spirit_tree_guard_times": {},
        "spirit_tree_guard_last_times": {},
        "spirit_tree_last_invasion_time": "",
        "spirit_tree_last_invasion_msg_id": 0,
    }


# =====================================================================
# 时间辅助函数
# =====================================================================

def now_str():
    """获取当前时间的标准格式字符串（北京时间）"""
    return datetime.now().strftime(TIME_FORMAT)

def str_to_dt(s):
    """标准格式时间字符串 → datetime 对象"""
    return datetime.strptime(s, TIME_FORMAT)

def dt_to_str(dt):
    """datetime 对象 → 标准格式时间字符串"""
    return dt.strftime(TIME_FORMAT)

def add_seconds_str(s, seconds):
    """
    给标准格式时间字符串加上指定秒数，返回新时间字符串。
    用于计算 CD 结束时间："假设现在出发，加上 CD 秒数后的时间点"
    """
    dt = str_to_dt(s)
    return dt_to_str(dt + timedelta(seconds=seconds))

def is_future(s):
    """
    判断一个标准格式时间字符串是否表示"未来的时间"。
    返回 True 表示该时间还没到，说明某个 CD/状态仍然有效。
    如果解析失败（格式错误等），保守地返回 False。
    """
    try:
        return str_to_dt(s) > datetime.now()
    except:
        return False

def seconds_until(s):
    """
    计算从当前时间到目标时间还有多少秒。
    如果目标时间已过，返回 0（不会返回负数）。
    """
    try:
        target = str_to_dt(s)
        diff = (target - datetime.now()).total_seconds()
        return max(0, diff)
    except:
        return 0

def scheduler_sleep_seconds(seconds, minimum=1):
    """Cap scheduler sleeps so loops re-check state and identity frequently."""
    try:
        seconds = float(seconds)
    except Exception:
        seconds = MAX_SCHEDULER_SLEEP_SECONDS
    return max(minimum, min(seconds, MAX_SCHEDULER_SLEEP_SECONDS))

def seconds_until_daily_task_start(now):
    """
    计算从当前时间到每日任务开始时间（早上 7:00）还有多少秒。
    如果当前时间 >= 7:00，返回 0（表示可以执行每日任务）。
    注意：这个函数返回的是"到下一个 7:00 的秒数"，
    如果现在是凌晨 3 点，返回 4 小时的秒数；
    如果现在是下午 3 点，返回 0（因为已经过了今天的 7 点，直接执行）。
    """
    target = now.replace(
        hour=DAILY_TASK_START_HOUR,
        minute=DAILY_TASK_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        return 0
    # +1 是为了避免恰好等于 0 导致立即执行，保证至少 1 秒的延迟
    return max(1, int((target - now).total_seconds()) + 1)

def daily_task_start_label():
    """返回每日任务开始时间的标签字符串（如 "07:00"）"""
    return f"{DAILY_TASK_START_HOUR:02d}:{DAILY_TASK_START_MINUTE:02d}"


# =====================================================================
# 日志系统初始化
# =====================================================================

# 启动前先修剪日志文件，防止日志无限增长占用磁盘
prune_log_file(LOG_FILE)

logging.basicConfig(
    level=logging.INFO,   # 默认日志级别为 INFO
    format='%(asctime)s [%(levelname)s] %(message)s',  # 时间 [级别] 内容
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),  # 追加写入文件
        logging.StreamHandler(sys.stdout),  # 同时输出到控制台
    ],
    force=True,
)
log = logging.getLogger('HuangFengGu_Main')     # 主日志记录器，名称用于区分不同脚本
logging.getLogger('telethon').setLevel(logging.WARNING)  # Telethon 库日志只显示 WARNING 以上
logging.getLogger('asyncio').setLevel(logging.WARNING)   # asyncio 库日志只显示 WARNING 以上


def load_config():
    """从 JSON 文件加载配置（API ID、API Hash、监控设置等）"""
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


# =====================================================================
# 日志过滤器
# =====================================================================

class ConnectionFilter(logging.Filter):
    """
    过滤掉 Telegram 连接相关的警告日志。
    Telethon 在重连时会打印 "Server closed the connection"，
    这些是正常的重连行为，不需要在日志中显示，避免干扰。
    """
    def filter(self, record):
        msg = record.getMessage()
        if "Server closed the connection" in msg:
            return False  # 丢弃这条日志
        return True

# 给所有日志 handler 添加过滤器
for handler in logging.root.handlers:
    handler.addFilter(ConnectionFilter())       # 过滤连接断开警告
    if isinstance(handler, logging.FileHandler):
        handler.addFilter(CommandLogFilter())   # 文件日志额外过滤指令内容（隐私保护）
logging.getLogger('telethon').addFilter(ConnectionFilter())  # Telethon 自身日志也过滤


# =====================================================================
# AtomicTaskContext: 整体性任务独占锁上下文管理器
# =====================================================================
class AtomicTaskContext:
    def __init__(self, cultivator, name="Task"):
        self.cultivator = cultivator
        self.name = name

    async def __aenter__(self):
        current_t = asyncio.current_task()
        while self.cultivator.active_atomic_task is not None and self.cultivator.active_atomic_task != current_t:
            await asyncio.sleep(0.5)
        self.cultivator.active_atomic_task = current_t
        log.info(f"🔒 [ATOMIC LOCK] Acquired by {self.name} (Task: {current_t.get_name() if hasattr(current_t, 'get_name') else id(current_t)})")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        current_t = asyncio.current_task()
        if self.cultivator.active_atomic_task == current_t:
            self.cultivator.active_atomic_task = None
            log.info(f"🔓 [ATOMIC LOCK] Released by {self.name} (Task: {current_t.get_name() if hasattr(current_t, 'get_name') else id(current_t)})")

# =====================================================================
# 主类：Cultivator
# =====================================================================

class Cultivator(CommonCommandMixin, ConcubineMixin, FishingMixin, YinluoMixin):
    """
    凌霄宫修仙主控类。
    继承自:
      - CommonCommandMixin: 通用固定冷却指令（如田野修炼、宗门战等）
      - ConcubineMixin: 侍妾相关操作
      - FishingMixin: 钓鱼流程
      - YinluoMixin: 阴罗宗阴罗幡流程

    职责: 管理所有修仙循环（登天阶、闭关、罡风、每日任务、元婴出窍等），
    通过 Telethon 客户端与 Telegram 游戏机器人交互。
    """

    def state_time_command_for_key(self, key):
        if key == "next_treasure_touch_time":
            return TREASURE_TOUCH_COMMAND
        if key == "next_nurture_spirit_time":
            return NURTURE_SPIRIT_COMMAND
        return CommonCommandMixin.state_time_command_for_key(self, key)

    def __init__(self, session_name='telegram_cli_session'):
        """
        初始化 Cultivator 实例。
        加载配置 → 建立 Telegram 客户端 → 初始化状态 → 设置反馈事件系统。

        参数:
            session_name: Telethon session 文件名（用于持久化登录态）
        """
        # ------ 1. 加载配置 ------
        self.account_key = "main"
        self.config = load_config()
        self.mc = self.config.get('monitor', {})  # monitor 配置段

        # ------ 2. Telegram 客户端 ------
        self.session_file = os.path.join(CONFIG_DIR, session_name)
        self.client = TelegramClient(self.session_file, self.config['api_id'], self.config['api_hash'])

        # ------ 3. 目标聊天/主题 ------
        self.target_chat_id = self.mc.get('chat_id', 1680975844)  # 游戏群 ID
        self.topic_id = self.mc.get('topic_id', 7310786)          # 游戏主题（Forum Topic）ID
        # 游戏机器人用户名，去掉 @ 前缀并转小写，方便后续比较
        self.watch_bot = self.mc.get('watch_bot', 'fanrenxiuxian_bot').lower().lstrip('@')
        self.notify_bot_username = self.config.get('notify_bot', 'waaiging_bot')  # 告警机器人

        # ------ 4. 宗派信息 ------
        self.sect_name = "凌霄宫"
        self.identity_sect_names = {
            "主魂": "凌霄宫",
            "无咎子": "天星宗",
            "缘生子": "阴罗宗",
            "素缘子": "星宫",
        }
        self.lingxiao_enabled = True

        # ------ 5. 运行控制 ------
        self.is_running = True                     # 控制所有循环的运行状态
        self.cmd_lock = asyncio.Lock()             # 异步锁，防止多个循环同时发指令导致冲突
        self.startup_done = asyncio.Event()        # 启动同步完成的信号量，所有循环等待它
        self.meditation_state_event = asyncio.Event()  # 被动闭关状态变化时唤醒闭关循环
        self.pause_event = asyncio.Event()          # 暂停/恢复控制（set=运行中, clear=暂停中）
        self.pause_event.set()                      # 默认运行中
        # 止/启管理员名单（只有这些人发"止"才生效）
        self.pause_admins = set(self.mc.get("pause_admins", [8219248252, -1004240160265, -1003809391782, -1003999815554]))  # 主魂(Waaiging)+分身

        # ---- 身外化身系统 ----
        self.avatars = ["无咎子", "缘生子", "素缘子"]
        self.avatar_identities = {
            "-1004240160265": "无咎子",
            "-1003809391782": "缘生子",
            "-1003999815554": "素缘子",
        }
        self.avatar_nicknames = {
            "无咎子": "天星雷总",
            "缘生子": "苦力怕不怕",
            "素缘子": "老爱同学",
        }
        self.avatar_usernames = {
            "wuxinglinggen": "无咎子",
            "kulipabp": "缘生子",
            "OldEinstein": "素缘子",
        }
        self.spirit_tree_avatar = SPIRIT_TREE_AVATAR
        self.identity_usernames = {
            "主魂": ["Weeguu"],
        }
        self.avatar_features = {
            "无咎子": {"meditation_prefix": ".推命", "training_prefix_commands": [".推命 探索", ".改命 探索"], "training_cmd": ".野外历练", "training_level": "深入", "dream_map": True, "heart_trial": True, "tower": True, "daily_checkin": True, "destiny": True, "yuanying_out": True, "rift_search": True},
            "缘生子": {"meditation_prefix": "", "training_cmd": ".野外历练", "training_level": "", "dream_map": True, "heart_trial": True, "tower": True, "spirit_tree_irrigation": True, "daily_checkin": True, "yuanying_out": True, "rift_search": True},
            "素缘子": {"meditation_prefix": "", "training_cmd": ".野外历练", "training_level": "", "dream_map": True, "heart_trial": True, "tower": True, "formation": False, "formation_assist": True, "star_gazing": True, "star_attraction": True, "daily_checkin": True},
        }
        # 化身 chat_id 映射（供 log_utils.log_manual_outgoing_if_needed 使用）
        self._avatar_chat_ids = {
            "-1004240160265": "无咎子",
            "-1003809391782": "缘生子",
            "-1003999815554": "素缘子",
        }
        self.avatar_send_lock = asyncio.Lock()
        self._avatar_loop_active = False  # 化身顺序循环是否活跃（阻止主循环操作）
        self._avatar_loop_count = 0       # 化身并发循环计数（支持多个化身同时运行）
        self.command_avatar_map = {}

        # ------ 6. 状态持久化 ------
        self.state_file = STATE_FILE
        self.state = self.load_state()             # 从文件加载持久化状态
        # startup: restore paused state
        if self.state.get("is_paused", False):
            self.pause_event.clear()
            log.info("Startup: is_paused=True, entering paused state.")

        # ---- 化身状态（依赖 self.state） ----
        self._current_identity = self.state.get("current_identity", "主魂")
        self._main_confirmed = (self._current_identity == "主魂")  # 启动时若上次为主魂则默认确认，否则强制对齐
        self._switch_lock = asyncio.Lock()  # 防止多个任务同时发送 .切换 主魂
        self.star_gazing_lock = asyncio.Lock()  # 观星操作互斥锁
        self.ensure_avatar_states()

        # ------ 7. 运行时缓存 ------
        self.last_deep_date = ""                   # 上次深度闭关的日期
        self.last_daily_date = ""                  # 上次执行每日任务的日期
        self.my_info = None                        # 当前账号的 Telegram 用户信息

        # ------ 8. 反馈事件系统 ------
        # 这是整个脚本的核心机制：发送指令后，通过 feedback_events 等待机器人回复。
        # 每个已发送的指令都有一个 asyncio.Event，当收到对应回复时 set() 它。
        self.feedback_events = {}                  # {消息ID: asyncio.Event}
        self.last_feedback_text = {}               # {消息ID: 回复文本}
        self.last_feedback_msg = {}                # {消息ID: 回复消息对象}
        self.feedback_commands = {}                # {消息ID: 发送的指令文本}
        self.feedback_sent_ts = {}                 # {消息ID: 发送时间戳（time.monotonic）}
        self.feedback_senders = {}                 # {消息ID: 指令发送者 sender_id，用于排除命令回声和审计}
        self.last_sent_id = None                   # 最后发送的消息 ID

        # ------ 9. 告警/关键词监控 ------
        self.notify_users = [u.lower() for u in self.mc.get('notify_users', [])]  # 要监控的用户
        self.keywords = [k.lower() for k in self.mc.get('keywords', [])]          # 要监控的关键词
        self.active_atomic_task = None             # 整体任务独占锁持有任务
        self.pending_formation_invite_msg = None   # 待回复的阵法邀请消息（化身助阵用）
        self.formation_assist_in_progress = False  # 实时助阵并发保护
        self._spirit_tree_harvest_tasks = {}       # 身份级灵树成熟后的一次性采摘任务
        self._spirit_tree_guard_tasks = {}         # 身份级古剑门来袭后的一次性守山任务

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def load_state(self):
        """
        从 JSON 文件加载持久化状态。
        如果文件不存在或解析失败，返回默认状态。

        默认状态包含所有玩法循环的 CD 时间、进度、计数器等。
        加载后会执行格式迁移和数据清理，保证旧版本状态文件能平滑升级。

        返回:
            dict: 状态字典
        """
        # 默认状态的完整定义
        default_state = {
            "date": datetime.now().strftime('%Y-%m-%d'),  # 当前日期，用于判断是否新的一天
            "done": [],                                      # 今日已完成的任务列表
            "sect_skill_count": 0,                           # 今日宗门传功次数
            "cloud_stairs_progress": "",                     # 云阶进度（如 "5 / 12 阶"）
            "completed_weeks": "",                           # 已完成周天数
            "next_stairs_time": "",                          # 下次可登天阶的时间
            "next_heart_time": "",                           # 下次可用问心台的时间
            "heart_platform_date": "",                       # 上次使用问心台的日期
            "last_heart_time": "",                           # 上次使用问心台的时间
            "deep_meditation_end_time": "",                  # 深度闭关结束时间
            "next_meditation_retry_time": "",                # 闭关重试时间（遇到未知回复后）
            "concubine_recalled_for_meditation": False,      # 是否已为闭关召回侍妾
            "concubine_recalled_time": "",                   # 召回侍妾的时间
            "nine_heaven_wind_cd_time": "",                  # 九天罡风冷却结束时间
            "last_wind_success_time": "",                    # 上次九天罡风成功时间
            "last_stairs_success_time": "",                  # 上次登天阶成功时间
            "last_yuanying_out_time": "",                    # 上次元婴出窍时间
            "next_yuanying_out_time": "",                    # 下次元婴出窍时间
            "yuanying_out_active": False,                    # 元婴是否在外游历中
            "yuanying_out_end_time": "",                     # 元婴归窍截止时间
            "last_rift_search_time": "",                     # 上次探寻裂缝时间
            "next_rift_search_time": "",                     # 下次探寻裂缝时间
            "last_treasure_touch_time": "",                  # 上次抚摸法宝时间
            "next_treasure_touch_time": "",                  # 下次抚摸法宝时间
            "next_nurture_spirit_time": "",                   # 下次温养器灵时间
            "level": "",
            "current_exp": None,
            "total_exp": None,
            "spirit_root": "",
            "is_paused": False,                               # 脚本是否被暂停（"止"指令）
            "pending_star_gazing_manifest_time": "",           # 待观星对应的显化整点
            "star_gazing_claimed_manifest_time": "",           # 本账号已指派观星的显化整点
            "star_gazing_claimed_avatar": "",                  # 本轮显化已指派的身份
        }
        # 合并继承的默认状态
        default_state.update(common_command_default_state())
        default_state.update(concubine_default_state())
        default_state.update(spirit_tree_default_state())

        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                    # 补充缺失的键（新版本增加的字段，旧状态文件没有）
                    for k in default_state:
                        if k not in s:
                            s[k] = default_state[k]
                    # 去重 done 列表，防止重复记录
                    if isinstance(s.get("done"), list):
                        s["done"] = list(dict.fromkeys(s["done"]))
                    # 强制格式迁移与数据清理
                    # 如果某个状态的记录时间已经过期（不是未来时间），进行清理
                    for k in ["last_stairs_time", "last_wind_time", "last_heart_time", "deep_meditation_end_time"]:
                        val = s.get(k)
                        if val and isinstance(val, str) and not is_future(val):
                            if k == "deep_meditation_end_time":
                                if not self.meditation_protected_until({"deep_meditation_end_time": val}):
                                    s[k] = ""  # 过期的闭关时间清空，让脚本重新查询
                    # 修正状态标志同步
                    self.ensure_meditation_guard_from_end_time(s)
                    s["in_deep_meditation"] = bool(self.meditation_guard_wait_seconds_for_state(s) > 0)
                    return s
            except Exception as e:
                log.error(f"Load State JSON Error: {e}")
        return default_state

    def save_state(self):
        """
        将当前状态持久化到 JSON 文件。
        每次关键操作（CD 更新、进度更新）后都应调用此函数。
        使用 ensure_ascii=False 保持中文可读，indent=2 美化格式。
        """
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Save State Error: {e}")

    # ------------------------------------------------------------------
    # 消息发送核心
    # ------------------------------------------------------------------

    async def send_to_game(self, message, reply_to=None):
        """
        向游戏群发送消息。
        这是所有指令发送的底层方法。

        流程：
          1. 确定回复目标（有指定则用指定，否则回复主题根消息）
          2. 等待机器人活动确认（防止发送过快被限流）
          3. 检查是否允许发送（重试次数、冷却等）
          4. 记录发送意图
          5. 发送消息
          6. 记录已发送消息
          7. 安排自动删除（保护隐私）

        参数:
            message: 要发送的文本
            reply_to: 回复的目标消息 ID（可选）

        返回:
            int | None: 发送成功的消息 ID，失败则返回 None
        """
        # 整体任务守卫：防止打断正在进行的共历心劫、观星等
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused(getattr(self, "current_identity", "主魂"), message):
            return None

        try:
            target_reply = reply_to if reply_to else self.topic_id
            # 等待机器人活动确认：确保游戏机器人处于活跃状态，消息不会丢失
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
            # 检查发送限制（如重试次数限制）
            if not command_send_allowed(self, message, log):
                return None
            # 记录发送意图（用于后续去重/匹配）
            remember_script_send_intent(self, message)
            # 发送消息到目标聊天
            msg = await self.client.send_message(self.target_chat_id, message, reply_to=target_reply)
            remember_script_sent_message(self, msg)
            record_command_sent(self, msg, message, identity=getattr(self, "current_identity", "主魂"), source="auto", reply_to=target_reply, logger=log)
            # 安排自动删除（120 秒后删除，保持聊天清洁）
            schedule_command_auto_delete(self, msg, text=message, logger=log)
            log.info(f"🟢 OUT:\n{message}")
            return msg.id
        except Exception as e:
            log.error(f"Send Error [{message}] reply_to={target_reply}: {e}")
            await _handle_telegram_send_protection(
                self, message, e, logger=log, identity=getattr(self, "current_identity", "主魂")
            )
            return None

    async def auto_delete(self, msg):
        """
        延迟删除消息的辅助方法。
        等待 120 秒后尝试删除消息，失败则静默忽略。
        用于清理脚本发送的指令，保护隐私减少聊天噪音。
        """
        await asyncio.sleep(120)
        try:
            await msg.delete()
        except:
            pass

    async def _send_and_wait_feedback_raw(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, suppress_no_response_alert=False):
        """内部发送方法（不获取 avatar_send_lock，已被外部调用方持有）"""
        try:
            return await send_and_wait_feedback_common(
                self, log, message, timeout=timeout, max_retries=max_retries,
                reply_to=reply_to, return_msg=return_msg, return_response_msg=return_response_msg,
                delete_after=delete_after, return_msg_role="sent",
                suppress_no_response_alert=suppress_no_response_alert,
            )
        except Exception as e:
            log.error(f"_send_and_wait_feedback_raw [{message[:40]}] crashed: {e}")
            return None

    async def send_and_wait_feedback(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, force_identity_check=False, suppress_no_response_alert=False, force_meditation_check=False):
        """
        发送指令并等待回复（带 avatar_send_lock 保护）。
        所有主魂业务通过此方法发送。如果当前身份不是主魂，或者主魂状态未确认，自动切回主魂再发送。
        """
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        if str(message or "").strip() == ".查看闭关" and not force_meditation_check:
            guarded_resp = self.early_meditation_check_response_for_state("主魂", self.state, log)
            if guarded_resp:
                return guarded_resp

        # 暂停阻断守卫
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused("主魂", message):
            return None

        yield_attempts = 0
        defer_started_at = None
        urgent_yield_attempts = 0
        urgent_defer_started_at = None
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            switched_this_iteration = False
            async with self.avatar_send_lock:
                # 主魂自动身份对齐：如果当前是分身身份，或者主魂未确认，先切回主魂
                if self.current_identity != "主魂" or not self._main_confirmed:
                    if not command_send_precheck(self, message, log, identity="主魂"):
                        log.info(f"Skip auto-switch to 主魂: main command is not sendable now ({message}).")
                        return None
                    fishing_wait = await self.fishing_switch_wait_or_raise_due(
                        self.current_identity, target_identity="主魂", command=message
                    )
                    if fishing_wait > 0:
                        log.info(
                            f"Auto-switch to 主魂 deferred: {self.current_identity} is fishing; "
                            f"wait {fishing_wait:.1f}s for .提竿 before switching."
                        )
                        should_yield = True
                        wait_sec_to_sleep = fishing_wait
                    if (
                        not should_yield
                        and
                        not force_identity_check
                        and self.current_identity in self.avatars
                    ):
                        wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                        if 0 <= wait_sec <= 60:
                            if defer_started_at is None:
                                defer_started_at = time.monotonic()
                            deferred_for = time.monotonic() - defer_started_at
                            if deferred_for < 60:
                                if yield_attempts == 0 or yield_attempts % 12 == 0:
                                    log.info(
                                        f"Auto-switch to 主魂 deferred: {self.current_identity} "
                                        f"has commands due in {wait_sec:.1f}s."
                                    )
                                yield_attempts += 1
                                should_yield = True
                                wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))
                            else:
                                log.info(
                                    f"Auto-switch to 主魂 proceeds after {deferred_for:.1f}s defer; "
                                    f"{self.current_identity} still reports due commands."
                                )

                    if not should_yield:
                        log.info(f"🔄 Auto switch back to 主魂 from {self.current_identity} (before main command: {message})")
                        switch_resp = await self._send_and_wait_feedback_raw(".切换 主魂", timeout=30, max_retries=2)
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""

                        # 检测是否被封禁
                        if getattr(self, "check_and_record_switch_ban", None):
                            if self.check_and_record_switch_ban(resp_str):
                                log.error(f"❌ Auto-switch back to 主魂 failed due to global command ban. Blocking main command: {message}")
                                return None

                        is_success = False
                        if self.current_identity == "主魂":
                            is_success = True
                            log.info("✅ Auto-switch back to 主魂 passively confirmed.")
                        if resp_str:
                            if "成功" in resp_str or "已切换" in resp_str or "当前操控" in resp_str or "主魂" in resp_str:
                                is_success = True

                        if is_success:
                            self.current_identity = "主魂"
                            self._main_confirmed = True
                            switched_this_iteration = True
                            log.info("✅ Auto-switch back to 主魂 confirmed; sending pending command immediately.")
                        else:
                            log.error(f"❌ Auto-switch back to 主魂 FAILED! Blocking main command: {message}. Response: {resp_str[:120]}")
                            return None

                if not should_yield and not switched_this_iteration:
                    critical_wait = self.time_critical_defer_wait("主魂", message, timeout=timeout)
                    if critical_wait >= 0:
                        if urgent_defer_started_at is None:
                            urgent_defer_started_at = time.monotonic()
                        urgent_deferred_for = time.monotonic() - urgent_defer_started_at
                        if urgent_deferred_for < 120:
                            if urgent_yield_attempts == 0 or urgent_yield_attempts % 12 == 0:
                                log.info(
                                    f"Main command [{message}] deferred: time-critical command due "
                                    f"in {critical_wait:.1f}s."
                                )
                            urgent_yield_attempts += 1
                            should_yield = True
                            wait_sec_to_sleep = max(
                                1,
                                min(10, critical_wait - 5 if critical_wait > 10 else critical_wait + 1),
                            )
                        else:
                            log.info(
                                f"Main command [{message}] proceeds after {urgent_deferred_for:.1f}s "
                                "time-critical defer."
                            )

                if not should_yield:
                    resp = await self._send_and_wait_feedback_raw(
                        message, timeout=timeout, max_retries=max_retries,
                        reply_to=reply_to, return_msg=return_msg, return_response_msg=return_response_msg,
                        delete_after=delete_after,
                        suppress_no_response_alert=suppress_no_response_alert,
                    )
                    return resp

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

    # ------------------------------------------------------------------
    # 回复解析工具
    # ------------------------------------------------------------------

    def parse_wait_time(self, text, find_min=False, line_identifier=None):
        """
        从游戏机器人的回复文本中解析等待/冷却时间（秒）。

        解析策略：
          1. 清理 markdown 和空格，使正则匹配更稳定
          2. 按行处理，如果指定了 line_identifier，只处理包含该标识符的行
          3. 每行分别匹配小时、分钟、秒，汇总为该行的总秒数
          4. 返回第一个匹配行的秒数，或 find_min=True 时返回最小值

        为什么这样设计：
          - 游戏回复格式不统一，有时是 "冷却剩余 3 小时 12 分钟"
          - 有时一个回复包含多段信息（如天阶状态），需要按行标识符筛选
          - find_min 用于当回复包含多个时间（如多个 CD）时取最小值

        参数:
            text: 游戏回复文本
            find_min: 是否返回所有匹配时间中的最小值
            line_identifier: 行标识符，只解析包含该字符串的行

        返回:
            int: 等待秒数，-1 表示未找到任何时间信息
        """
        if not text:
            return -1
        # 先清理所有 markdown 加粗标记和空格，避免 **13** 分钟 这种格式干扰正则
        clean = text.replace('**', '').replace(' ', '')
        # 按行搜索，找到包含时间单位的行（避免误匹配修为数值等纯数字）
        timed_list = []
        for line in clean.split('\n'):
            # 如果指定了行标识符，只处理包含该标识符的行
            # 例如解析天阶状态时，只找包含"登阶冷却"的行
            if line_identifier and line_identifier not in line:
                continue
            h = re.search(r'(\d+)(?:小时|h)', line)   # 匹配 "X小时" 或 "Xh"
            m = re.search(r'(\d+)(?:分钟|分|m)', line) # 匹配 "X分钟" 或 "X分" 或 "Xm"
            s = re.search(r'(\d+)(?:秒|s)', line)      # 匹配 "X秒" 或 "Xs"
            sec = 0
            found = False
            if h:
                sec += int(h.group(1)) * 3600
                found = True
            if m:
                sec += int(m.group(1)) * 60
                found = True
            if s:
                sec += int(s.group(1))
                found = True
            if found:
                timed_list.append(sec)
        if not timed_list:
            return -1
        return min(timed_list) if find_min else timed_list[0]

    def is_loose_feedback_candidate(self, command, text):
        """
        判断一条消息是否可能是某个指令的"宽松匹配"回复。

        有些游戏回复不是直接回复（reply_to）我们的消息，也不包含 @提及，
        但仍然是针对我们的指令的回复（例如登天阶结果发到主题根消息）。
        这个函数通过关键词匹配来做"模糊"判断。

        匹配层级：
          1. 特定指令精确匹配（.切换、.查看闭关、.启阵、.登天阶）
          2. 通用冷却/响应关键词兜底（所有 . 指令）

        参数:
             command: 我们发送的指令（如 ".登天阶"）
             text: 收收到消息文本

        返回:
             bool: 是否可能是该指令的回复
        """
        if not text or not command:
            return False
        # 层级1：特定指令精确匹配
        if command.startswith(".切换 "):
            parts = command.strip().split()
            if len(parts) >= 2:
                target = parts[1]
                has_success = any(k in text for k in ["成功", "已切换", "当前操控", "神念重归", "切换成功"])
                if has_success and target in text:
                    return True
        if command == ".查看闭关":
            return (
                is_deep_meditation_ongoing_response(text)
                or is_deep_meditation_settlement_response(text)
                or is_not_deep_meditation_response(text)
            )
        if command == ".状态":
            return "修士状态" in text and "境界" in text
        if command == ".我的灵根":
            return "天命玉牒" in text and "修为" in text
        if command in {SPIRIT_TREE_IRRIGATION_COMMAND, SPIRIT_TREE_STATUS_COMMAND, SPIRIT_TREE_HARVEST_COMMAND, SPIRIT_TREE_GUARD_COMMAND}:
            return any(k in text for k in [
                "灵树", "灵果", "采摘期", "成熟采摘期", "造化青莲果",
                "灵眼之树", "成熟度", "灌溉", "采摘", "修为增长",
                "协同守山", "守山", "护山", "古剑门", "加固",
            ])
        if command in {".启阵", ".助阵"}:
            formation_keywords = [
                "冷却", "再次启阵", "心神消耗", "参与过布阵",
                "周天星斗大阵", "布设大阵",
                "助阵", "同门相助", "60秒",
                "修为不足", "正在布阵",
            ]
            return any(k in text for k in formation_keywords)
        if command == ".登天阶":
            if "【凌霄云阶】" in text:
                return True
            if "登阶冷却" in text:
                return True
            if "九天罡风" in text and any(k in text for k in ["尚未", "未再聚", "后再", "后再试", "冷却"]):
                return True
        # 层级2：通用冷却/响应关键词兜底（所有 . 指令）
        if command.startswith("."):
            general_keywords = [
                "冷却", "冷却中", "冷却时间",       # 冷却
                "后再", "后再试", "方可",            # 时间限制
                "剩余", "不足", "无法", "尚未",      # 不可用
                "已达上限", "已经", "已完成", "已经完成",  # 已完成
                "修为不足", "境界不足",              # 条件不足
                "正在", "进行中",                    # 进行中
                "成功", "获得", "增加了", "减少了",   # 成功结果
                "心浮气躁", "需要打坐", "调息",       # 闭关相关
                "未知指令", "不存在",                # 错误
            ]
            return any(k in text for k in general_keywords)
        return False

    def is_topic_root_or_plain_message(self, msg):
        """
        判断消息是否是主题根消息（直接发到主题，而不是回复某人）。
        在 Forum Topic 中，回复根消息的消息 msg.reply_to 可能为空或指向 topic_id。

        参数:
            msg: Telegram 消息对象

        返回:
            bool: 是否是主题根消息
        """
        if not msg.reply_to:
            return True
        replied_id = getattr(msg.reply_to, 'reply_to_msg_id', None)
        return replied_id == self.topic_id

    # ------------------------------------------------------------------
    # 宗门传功响应处理
    # ------------------------------------------------------------------

    def record_sect_skill_response(self, resp):
        """
        解析宗门传功的回复，更新已传功次数。

        传功回复有多种格式：
          - "今日已传功 1 / 3"（含计数）
          - "次数不足"/"明日再来"/"已经"（已满）
          - "失败"/"需回复"/"主魂"（目标无效）
          - "成功"/"元神"/"传功"（传功成功）
          - 无法识别 -> 记录警告

        参数:
            resp: 游戏回复文本

        返回:
            str: "counted" 已计数、"done" 已满、"invalid" 无效目标、
                 "unknown" 无法识别
        """
        return self.common_record_sect_skill_response(resp, max_daily=SECT_SKILL_MAX_DAILY)

    # ------------------------------------------------------------------
    # 游戏消息处理核心
    # ------------------------------------------------------------------

    async def handle_game_response(self, event):
        """
        游戏消息事件处理器。
        每当游戏群有新消息时触发，是所有消息处理的入口。

        处理流程：
          1. 消息预处理（日志、反机器人、自动回复等）
          2. 关键词/BOSS 监测（通知用户）
          3. 反馈匹配（寻找与已发送指令匹配的回复）
             a) 精确匹配：消息回复了我们的某条消息
             b) 宽松匹配：消息提到了我们的用户名
             c) 模糊匹配：消息内容符合某个指令的回复特征，且发送者是游戏机器人
          4. 未匹配消息记录

        参数:
            event: Telethon 的 NewMessage 事件对象
        """
        try:
            msg = event.message
            text = (msg.text or "")
            msg_text_lower = text.lower()
            sender_id = msg.sender_id

            # ---- 控制指令：止/启（仅管理员可触发） ----
            sender_check = await event.get_sender()
            # chat_id 比较需兼容 Telethon 的 -100 前缀（supergroup）
            _chat_id_match = (msg.chat_id == self.target_chat_id or msg.chat_id == int(f"-100{self.target_chat_id}"))
            if not is_game_bot_sender(self, sender_check) and _chat_id_match:
                if await handle_clear_history_command(self, msg, text, sender_check, log):
                    return
                if await self.maybe_handle_fishing_control_message(msg, text, sender_check):
                    return
                if sender_is_pause_admin(self, msg):
                    stripped = text.strip()
                    if stripped in ("止", ".止", "0", ".0"):
                        if self.pause_event.is_set():
                            self.pause_event.clear()
                            self.state["is_paused"] = True
                            self.save_state()
                            log.info("⏸️ PAUSE command received. All loops paused.")
                            await self.client.send_message(8219248252, "⏸️ 凌霄宫脚本已暂停。发送「1」恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return
                    elif stripped in ("启", ".启", "1", ".1"):
                        if not self.pause_event.is_set():
                            self.pause_event.set()
                            self.state["is_paused"] = False
                            self.save_state()
                            log.info("▶️ RESUME command received. All loops resumed.")
                            await self.client.send_message(8219248252, "▶️ 凌霄宫脚本已恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return

            sender_cache = await event.get_sender()
            record_message_event(self, msg, text=text, sender=sender_cache, event_kind="new", direction="raw", logger=log)
            record_star_gazing_event("main", msg, text, sender=sender_cache, logger=log)

            # 如果是脚本自己手动发出的消息，记录但不处理
            if log_manual_outgoing_if_needed(self, msg, text=text):
                return
            if await maybe_handle_han_soul_choice(self, msg, text, sender_cache, log):
                return
            if not sender_id:
                return

            # 记录游戏机器人活动（用于判断机器人是否在线）
            if is_game_bot_sender(self, sender_cache):
                record_game_bot_activity(self, sender_cache, log)
                self.record_star_gazing_final_report_if_needed(msg, text, source="new message")
                # 被动身份自愈 + 手动指令状态同步
                self.update_identity_passively(msg)
                manual_reply = is_reply_to_manual_command(self, msg)
                manual_processed = await record_manual_command_reply_state_if_needed(self, msg, text, sender_cache, log)
                self.record_star_shift_attempt_if_needed(msg, text, source="new message")
                if not manual_reply or not manual_processed:
                    self.maybe_record_spirit_tree_passive_message(msg, text, source="new message")
                if not manual_reply:
                    self.maybe_record_avatar_passive_states(msg)
                await self.maybe_record_fishing_rod_message(msg, text, sender_cache)

                # 星宫化身专属：全天候被动截获好星相
                await self.maybe_handle_star_gazing_opportunity(msg, text, sender_cache)
                if self.is_target_formation_invite(text):
                    asyncio.create_task(self.maybe_assist_target_formation_invite(msg))

            # 处理反机器人验证（如 captcha）
            if await handle_anti_bot_challenge(self, msg, text, sender_cache, log, title="凌霄宫自证告警"):
                return

            # 被动记录田野修炼和宗门战信息
            self.maybe_record_field_training_passive(msg, text)
            self.maybe_handle_sect_war_message(msg, text, sender_cache)

            # 如果是自动回复的后续消息，交给 auto_reply_features 处理
            if is_auto_reply_followup(self, msg, sender=sender_cache):
                return

            # ---- BOSS / 关键词监测 ----
            # 监控群消息中的关键词（如 "BOSS"、"天劫" 等），
            # 同时匹配通知用户列表，防止误报
            has_kw = any(k in msg_text_lower for k in self.keywords)
            has_user = mentions_self(self, msg, text) or any(
                f"@{u.lower()}" in msg_text_lower or f"【{u.lower()}】" in msg_text_lower
                for u in self.notify_users
            )
            if has_kw and has_user:
                sender_cache = await event.get_sender()
                sender = sender_cache
                sender_username = (getattr(sender, "username", "") or "").lower().lstrip('@')
                # 忽略非机器人发送的"BOSS"消息（可能是玩家在讨论，不是真正的 BOSS 出现）
                if not is_game_bot_sender(self, sender):
                    log.info(f"Boss-like message ignored from @{sender_username or sender_id}: {text[:80]}...")
                else:
                    log.info(f"!!! BOSS ALERT DETECTED !!!")
                    alert_text = f"【BOSS提醒】监测到凡人修仙 BOSS：\n{text}"

                    bot_token = self.config.get('notify_bot_token')
                    target = self.config.get('notify_target', 'Waaiging')

                    try:
                        # 处理目标格式：如果只是用户名，补 @
                        final_target = target
                        if isinstance(target, str) and not target.startswith('@') and not target.lstrip('-').isdigit():
                            final_target = f"@{target}"

                        # 限制告警文本长度，防止 Telegram API 拒绝
                        if len(alert_text) > 4000:
                            alert_text = alert_text[:4000] + "..."

                        # 优先通过机器人 API 发送告警（更稳定，不需要客户端在线）
                        sent_via_bot = False
                        if bot_token and "在这里填入" not in bot_token:
                            import urllib.request
                            import urllib.error
                            import json
                            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                            data = json.dumps({"chat_id": final_target, "text": alert_text}).encode('utf-8')
                            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
                            try:
                                with urllib.request.urlopen(req, timeout=5) as response:
                                    log.info(f"Alert sent via BOT to {final_target}")
                                    sent_via_bot = True
                            except Exception as be:
                                log.error(f"Bot Notify Exception: {be}. Will fallback to Client.")

                        # 如果机器人 API 发送失败，用客户端发送
                        if not sent_via_bot:
                            await self.client.send_message(final_target, alert_text)
                            log.info(f"Alert sent via Client to {final_target}")
                    except Exception as e:
                        log.error(f"Notify Final Error: {e}")

            if await maybe_auto_reply_exchange(self, event, text=text, sender=sender_cache):
                return

            # ---- 反馈匹配（核心机制） ----
            is_matched = False

            # 1) 精确匹配：优先使用 Telegram reply_to 归属到待处理命令
            is_matched = match_pending_feedback_by_reply(
                self, msg, text, self.is_loose_feedback_candidate, log, label="[REPLY-FEEDBACK]"
            )

            # 2) 宽松匹配：消息提到了我们的用户名，但没有回复我们的消息
            #    适用于某些游戏回复不准确的情况
            if not is_matched:
                # 如果是对手动指令的回复，跳过宽松匹配（防止截胡）
                if is_reply_to_manual_command(self, msg):
                    log.info(f"Manual command response detected in loose match (msg {msg.id}), skipping.")
                    is_matched = True
                elif (
                    self.feedback_events
                    and is_game_bot_sender(self, sender_cache)
                    and text_targets_current_account(self, msg, text)
                ):
                    is_matched = match_pending_edited_feedback(
                        self,
                        msg,
                        text,
                        self.is_loose_feedback_candidate,
                        log,
                        id_window=20,
                        label="[MENTION-FEEDBACK]",
                    )

            # 3) 模糊匹配：没有回复我们的消息，也没有 @我们，但内容明显是某个指令的回复
            #    某些游戏机器人把回复发到主题根消息而不是作为 reply
            if not is_matched and self.feedback_events and self.is_topic_root_or_plain_message(msg):
                sender_cache = sender_cache or await event.get_sender()
                if is_game_bot_sender(self, sender_cache):
                    # 如果是对手动指令的回复，不进行模糊匹配（防止截胡）
                    if is_reply_to_manual_command(self, msg):
                        log.info(f"Manual command response detected (msg {msg.id}), skipping fuzzy match.")
                        is_matched = True  # 标记为已处理，避免后续误匹配
                    else:
                        is_matched = match_pending_edited_feedback(
                            self,
                            msg,
                            text,
                            self.is_loose_feedback_candidate,
                            log,
                            id_window=20,
                            label="[LOOSE-FEEDBACK]",
                        )

            # 未匹配的消息：记录 @提及，处理自动回复
            if not is_matched:
                log_mention_if_needed(self, msg, text=text, sender=sender_cache)
                if await maybe_auto_reply_exchange(self, event, text=text):
                    return
        except Exception as e:
            log.error(f"Error in handle_game_response: {e}")

    # ------------------------------------------------------------------
    # 云阶进度管理
    # ------------------------------------------------------------------

    def get_cloud_stairs_step(self):
        """
        从缓存的状态中获取当前云阶步数。
        格式如 "5 / 12 阶"，提取数字部分。

        返回:
            int: 当前阶数（0-12），解析失败返回 0
        """
        current_progress = self.state.get("cloud_stairs_progress", "0 / 12")
        try:
            return int(re.search(r'(\d+)', current_progress).group(1))
        except Exception:
            return 0

    def update_completed_weeks_from_text(self, text, source="Cloud stairs"):
        """
        从游戏回复文本中解析"已完成周天"轮数，更新到状态。

        两种格式：
          - "已完成周天：3 轮"
          - "完成了第 2 轮"

        参数:
            text: 游戏回复文本
            source: 来源描述（用于日志）

        返回:
            bool: 是否成功解析到轮数
        """
        if not text:
            return False

        clean_text = text.replace('**', '')
        patterns = [
            r'已完成周天[：:]\s*(\d+)\s*轮',
            r'完成了第\s*(\d+)\s*轮',
        ]
        for pattern in patterns:
            match = re.search(pattern, clean_text)
            if match:
                self.state["completed_weeks"] = f"{match.group(1)} 轮"
                log.info(f"{source}: Completed weeks synced to {match.group(1)}")
                return True
        return False

    def get_completed_weeks_count(self):
        """Return the cached completed cloud-stairs round count."""
        value = str(self.state.get("completed_weeks", "") or "")
        match = re.search(r'(\d+)', value)
        return int(match.group(1)) if match else 0

    def cloud_stairs_status_needs_refresh(self, next_stairs=None):
        """
        Return whether startup sync should refresh cloud-stairs status when local
        cooldown data is stale.

        The climb loop intentionally does not use this preflight anymore; .登天阶
        replies are authoritative for progress and cooldown updates.
        """
        if not self.state.get("cloud_stairs_progress"):
            return True
        if next_stairs is None:
            next_stairs = self.restore_cloud_stairs_time_from_last()
        if not next_stairs:
            return True
        return isinstance(next_stairs, str) and not is_future(next_stairs)

    def update_cloud_stairs_progress_from_text(self, text, source="Cloud stairs"):
        """
        从游戏回复文本中解析云阶当前进度，更新到状态。

        两种格式：
          - "当前进度 5/12 阶"
          - "踏上了第 5 阶云阶"

        参数:
            text: 游戏回复文本
            source: 来源描述（用于日志）

        返回:
            bool: 是否成功解析到进度
        """
        if not text:
            return False

        # 先尝试解析已完成周天轮数
        self.update_completed_weeks_from_text(text, source=source)

        clean_text = text.replace('**', '').replace(' ', '')
        # 格式1: "当前进度 5/12 阶"
        progress_match = re.search(r'当前(?:云阶)?进度(?:仍为)?[：:]?(\d+)/(\d+)阶?', clean_text)
        if progress_match:
            current = progress_match.group(1)
            total = progress_match.group(2)
        else:
            # 格式2: "踏上了第 5 阶云阶"
            climb_match = re.search(r'踏上了?第(\d+)阶云阶', clean_text)
            if not climb_match:
                return False
            current = climb_match.group(1)
            # 保留原有的总阶数（如果没有就默认 12）
            existing_total = re.search(r'/\s*(\d+)', self.state.get("cloud_stairs_progress", ""))
            total = existing_total.group(1) if existing_total else "12"

        self.state["cloud_stairs_progress"] = f"{current} / {total} 阶"
        log.info(f"{source}: Cloud stairs progress synced to {current}/{total}")
        return True

    def is_lingxiao_identity_mismatch_response(self, text):
        clean = str(text or "").replace("**", "")
        return "你并非凌霄宫弟子" in clean or "云阶禁制不会为你显现" in clean

    def record_cloud_stairs_response(self, stairs_resp, source="Cloud stairs climb"):
        """
        解析登天阶的回复并更新状态。

        三种响应模式：
          1. 登阶成功（包含【凌霄云阶】或"踏上"等关键词）
             -> 更新进度、更新周天轮数、设置 CD
          2. 冷却中（包含冷却时间）
             -> 解析 CD 并设置 next_stairs_time
          3. 不可用但无 CD 信息
             -> 设置 10 分钟保底重试

        参数:
            stairs_resp: 游戏回复文本
            source: 来源描述

        返回:
            bool: 是否成功登阶
        """
        if not stairs_resp:
            return False

        if self.is_lingxiao_identity_mismatch_response(stairs_resp):
            self._main_confirmed = False
            self.state["next_stairs_time"] = add_seconds_str(now_str(), 120)
            self.save_state()
            log.warning(f"{source}: main identity mismatch before .登天阶; will force .切换 主魂 before retry.")
            return False

        # ---- 登阶成功 ----
        if (
            "【凌霄云阶】" in stairs_resp
            or ("踏上" in stairs_resp and "云阶" in stairs_resp)
            or ("当前云阶进度" in stairs_resp and any(k in stairs_resp for k in ["本次获得", "额外收获", "修为", "宗门贡献", "罡风淬体"]))
        ):
            self.update_cloud_stairs_progress_from_text(stairs_resp, source=source)
            self.update_completed_weeks_from_text(stairs_resp, source=source)

            now = now_str()
            self.state["last_stairs_time"] = now
            self.state["last_stairs_success_time"] = now
            self.state["next_stairs_time"] = add_seconds_str(now, CLOUD_STAIRS_CD_SECONDS)
            self.record_daily_reward_event("主魂", ".登天阶", stairs_resp, source=source)
            log.info(f"Cloud stairs success, next run at {self.state['next_stairs_time']}")
            return True

        # ---- 被九天罡风冷却挡住 ----
        # 游戏会在 .登天阶 时返回“九天罡风尚未再聚”，这对登阶循环来说
        # 也是下一次登阶时间，必须同时写回登阶和罡风 CD。
        if "九天罡风" in stairs_resp and any(k in stairs_resp for k in ["尚未", "未再聚", "后再", "后再试", "冷却"]):
            cd = self.parse_wait_time(stairs_resp)
            if cd > 0:
                next_time = add_seconds_str(now_str(), cd)
                self.state["next_stairs_time"] = next_time
                self.state["nine_heaven_wind_cd_time"] = next_time
                log.info(f"{source}: Cloud stairs blocked by Wind CD ({cd}s), next run at {next_time}")
                self.save_state()
                return False

        # ---- 冷却中 ----
        # 先尝试从含"登阶冷却"标识的行解析
        cd = self.parse_wait_time(stairs_resp, line_identifier="登阶冷却")
        # 如果没找到冷却行，但回复包含冷却相关关键词，尝试全局解析
        if cd <= 0 and any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            cd = self.parse_wait_time(stairs_resp)

        if cd > 0:
            self.state["next_stairs_time"] = add_seconds_str(now_str(), cd)
            log.info(f"Cloud stairs CD from response: {cd}s, next run at {self.state['next_stairs_time']}")
            return False

        # ---- 不可用但无 CD 信息 ----
        if any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            log.warning(f"Cloud stairs appears unavailable but no CD parsed: {stairs_resp[:100]}")
            notify_unrecognized_response(self, ".登天阶", stairs_resp, log, "登天阶冷却解析")
            self.state["next_stairs_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False

        # ---- 完全无法识别的回复 ----
        notify_unrecognized_response(self, ".登天阶", stairs_resp, log, source)
        self.state["next_stairs_time"] = add_seconds_str(now_str(), 600)
        self.save_state()
        return False

    def restore_cloud_stairs_time_from_last(self):
        """
        从上次成功时间推断下次可登天阶时间。
        如果 next_stairs_time 为空/过期，或早于上次成功时间推断出的 CD，
        用 last_stairs_time + 3小时 推断 next_stairs_time。

        这样即使状态文件丢失或被旧缓存覆盖了 next_stairs_time，也能恢复 CD 信息。

        返回:
            str: next_stairs_time（原始或推断的）
        """
        last_stairs = self.state.get("last_stairs_time", "") or self.state.get("last_stairs_success_time", "")
        next_stairs = self.state.get("next_stairs_time", "")
        if last_stairs:
            inferred = add_seconds_str(last_stairs, CLOUD_STAIRS_CD_SECONDS)
            if is_future(inferred):
                should_restore = not next_stairs or not is_future(next_stairs)
                if not should_restore:
                    try:
                        should_restore = str_to_dt(inferred) > str_to_dt(next_stairs)
                    except Exception:
                        should_restore = True
                if should_restore:
                    self.state["next_stairs_time"] = inferred
                    self.save_state()
                    log.info(f"Cloud stairs next restored from last success: {inferred}")
                    return inferred
        if next_stairs:
            return next_stairs
        return next_stairs

    # ------------------------------------------------------------------
    # 九天罡风 / 问心台 状态管理
    # ------------------------------------------------------------------

    def is_nine_heaven_wind_ready(self):
        """
        判断九天罡风是否冷却完毕，可以使用。

        返回:
            bool: True = 冷却完毕，可以施展
        """
        if self.get_completed_weeks_count() < 1:
            return False
        wind_cd_time = self.state.get("nine_heaven_wind_cd_time", 0)
        # 如果没有 CD 时间记录，或者 CD 时间已过（不是未来时间），则可用
        return not wind_cd_time or (isinstance(wind_cd_time, str) and not is_future(wind_cd_time))

    def has_pending_wind_buff(self):
        """
        判断是否还有未使用的九天罡风 buff。

        逻辑：如果上次罡风成功时间 > 上次登阶成功时间，
        说明罡风 buff 还在（登阶还没消耗它）。

        返回:
            bool: True = 有未使用的风 buff
        """
        last_wind = self.state.get("last_wind_time") or self.state.get("last_wind_success_time", "")
        last_stairs = self.state.get("last_stairs_time") or self.state.get("last_stairs_success_time", "")
        return bool(last_wind and (not last_stairs or last_wind > last_stairs))

    def has_pending_heart_buff(self):
        """
        判断是否还有未使用的问心台 buff。

        逻辑同 has_pending_wind_buff: 如果问心台时间 > 登阶时间，
        说明 buff 还在。

        返回:
            bool: True = 有未使用的问心台 buff
        """
        last_heart = self.state.get("last_heart_time", "")
        last_stairs = self.state.get("last_stairs_time") or self.state.get("last_stairs_success_time", "")
        return bool(last_heart and (not last_stairs or last_heart > last_stairs))

    def is_nine_heaven_wind_round_locked_response(self, text):
        """九天罡风未满足周天轮数要求，不属于冷却解析失败。"""
        clean = str(text or "").replace("**", "")
        return (
            "九天罡风" in clean
            and ("周天" in clean or "轮" in clean)
            and any(k in clean for k in ["尚未完成", "无法承受", "未解锁", "需完成", "需 "])
        )

    def next_nine_heaven_wind_round_probe_time(self):
        """Probe wind again after the next cloud-stairs opportunity, or shortly if unknown."""
        next_stairs = self.restore_cloud_stairs_time_from_last()
        if isinstance(next_stairs, str) and is_future(next_stairs):
            return add_seconds_str(next_stairs, 60)
        return add_seconds_str(now_str(), 600)

    def defer_nine_heaven_wind_round_locked(self, source="Nine Heaven Wind"):
        next_probe = self.next_nine_heaven_wind_round_probe_time()
        self.state["nine_heaven_wind_cd_time"] = next_probe
        log.info(f"{source}: Wind locked by 周天 requirement; next probe at {next_probe}.")
        self.save_state()
        return False

    def defer_nine_heaven_wind_if_round_locked(self, source="Nine Heaven Wind"):
        if self.get_completed_weeks_count() >= 1:
            return False
        self.defer_nine_heaven_wind_round_locked(source)
        return True

    def heart_platform_fallback_time(self, today):
        """
        问心台每日保底执行时间：当天 23:50。
        如果一天快结束了还没用问心台，在这个时间强制使用。

        参数:
            today: 日期字符串 "YYYY-MM-DD"

        返回:
            str: 保底执行时间字符串
        """
        return f"{today} 23:50:00"

    def is_heart_platform_fallback_due(self, today):
        """
        判断是否已经到了问心台每日保底执行时间。

        参数:
            today: 日期字符串 "YYYY-MM-DD"

        返回:
            bool: 是否到了保底时间
        """
        return datetime.now() >= str_to_dt(self.heart_platform_fallback_time(today))

    def is_heart_platform_throttled(self):
        """
        判断问心台是否在冷却中（距上次使用不足 3 小时）。
        如果是，设置 next_heart_time 并返回 True。

        返回:
            bool: True = 在冷却中，不应使用
        """
        last_heart = self.state.get("last_heart_time", "")
        if not last_heart:
            return False
        elapsed = (datetime.now() - str_to_dt(last_heart)).total_seconds()
        if elapsed < HEART_PLATFORM_MIN_INTERVAL_SECONDS:
            next_allowed = add_seconds_str(last_heart, HEART_PLATFORM_MIN_INTERVAL_SECONDS)
            self.state["next_heart_time"] = next_allowed
            log.info(f"Heart Platform skipped: last use at {last_heart}, next allowed at {next_allowed}.")
            self.save_state()
            return True
        return False

    def heart_platform_already_used_today(self, today):
        """
        判断问心台今天是否已经使用过。
        避免一天内多次使用问心台（游戏设定每天只能一次）。

        参数:
            today: 日期字符串 "YYYY-MM-DD"

        返回:
            bool: True = 今天已经用过
        """
        if self.state.get("heart_platform_date") == today:
            return True
        last_heart = self.state.get("last_heart_time", "")
        if last_heart.startswith(today):
            # 状态中的日期信息与 last_heart 日期一致，同步更新
            self.state["heart_platform_date"] = today
            self.state["next_heart_time"] = add_seconds_str(f"{today} 00:05:00", 24 * 3600)
            self.save_state()
            log.info(f"Heart Platform skipped: last use already recorded today at {last_heart}.")
            return True
        return False

    # ------------------------------------------------------------------
    # 九天罡风响应处理
    # ------------------------------------------------------------------

    def record_nine_heaven_wind_response(self, wind_resp, source="Nine Heaven Wind"):
        """
        解析九天罡风的回复并更新状态。

        三种响应模式：
          1. 成功施展（关键词：成功、施展、罡风、淬体）
             -> 设置 12 小时 CD，记录成功时间
          2. 冷却中
             -> 解析 CD 时间，推断最后成功时间用于 buff 判断
          3. 不可用但无 CD 信息
             -> 设置 10 分钟保底重试

        参数:
            wind_resp: 游戏回复文本
            source: 来源描述

        返回:
            bool: 是否成功施展
        """
        if not wind_resp:
            return False

        if self.is_lingxiao_identity_mismatch_response(wind_resp):
            self._main_confirmed = False
            self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now_str(), 120)
            self.save_state()
            log.warning(f"{source}: main identity mismatch before .引九天罡风; will force .切换 主魂 before retry.")
            return False

        if self.is_nine_heaven_wind_round_locked_response(wind_resp):
            return self.defer_nine_heaven_wind_round_locked(source)

        # ---- 冷却中 ----
        # 先按行标识符"引九天罡风"解析，再按"罡风"解析，最后全局解析
        cd = self.parse_wait_time(wind_resp, line_identifier="引九天罡风")
        if cd <= 0:
            cd = self.parse_wait_time(wind_resp, line_identifier="罡风")
        if cd <= 0 and any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            cd = self.parse_wait_time(wind_resp)

        if cd > 0:
            now = datetime.now()
            self.state["nine_heaven_wind_cd_time"] = dt_to_str(now + timedelta(seconds=cd))
            # 推断最后成功时间：冷却时间 < 12 小时说明刚刚施展过
            # 通过"current_time + CD - 12h"反推成功时间点
            if cd <= NINE_HEAVEN_WIND_CD_SECONDS:
                inferred_success = now + timedelta(seconds=cd) - timedelta(seconds=NINE_HEAVEN_WIND_CD_SECONDS)
                current_last = self.state.get("last_wind_time") or self.state.get("last_wind_success_time", "")
                if not current_last or inferred_success > str_to_dt(current_last):
                    inferred_str = dt_to_str(inferred_success)
                    self.state["last_wind_time"] = inferred_str
                    self.state["last_wind_success_time"] = inferred_str
                    log.info(f"{source}: Wind last success inferred from CD as {inferred_str}")
            log.info(f"{source}: Wind CD from response: {cd}s")
            self.save_state()
            return False

        # ---- 不可用但无 CD ----
        if any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            log.warning(f"{source}: Wind appears unavailable but no CD parsed: {wind_resp[:100]}")
            notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, f"{source} 冷却解析")
            self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False

        # ---- 成功施展 ----
        if any(k in wind_resp for k in ["成功", "施展", "罡风", "淬体"]):
            cd_seconds = NINE_HEAVEN_WIND_CD_SECONDS
            now = now_str()
            self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now, cd_seconds)
            self.state["last_wind_time"] = now
            self.state["last_wind_success_time"] = now
            log.info(f"{source}: Wind success, next run in {cd_seconds}s")
            self.save_state()
            return True

        # ---- 无法识别 ----
        log.warning(f"{source}: Wind response unusual: {wind_resp[:100]}")
        notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, source)
        self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now_str(), 600)
        self.save_state()
        return False

    # ------------------------------------------------------------------
    # 侍妾闭关相关（已禁用 .召回侍妾）
    # ------------------------------------------------------------------

    async def recall_concubine_before_meditation_end(self):
        """
        【已禁用】闭关结束前召回侍妾。
        当前版本不执行任何操作，直接跳过并将状态清零。
        """
        log.info("Meditation: .召回侍妾 disabled; skipping concubine recall.")
        self.state["concubine_recalled_for_meditation"] = False
        self.state["concubine_recalled_time"] = ""
        self.save_state()

    async def place_concubine_after_meditation_start(self):
        """
        【已禁用/条件性】深度闭关开始后安置侍妾。
        仅当之前有召回记录时才执行 .安置侍妾。
        """
        if not self.state.get("concubine_recalled_for_meditation"):
            return
        log.info("Meditation: Deep meditation started, sending .安置侍妾.")
        await self.send_and_wait_feedback(".安置侍妾")
        self.state["concubine_recalled_for_meditation"] = False
        self.state["concubine_recalled_time"] = ""
        self.save_state()

    # ------------------------------------------------------------------
    # 闭关等待工具
    # ------------------------------------------------------------------

    async def sleep_until_meditation_check(self, end_time, label="Meditation"):
        """
        等待到闭关结束时间之后（再加 30-60 秒随机延迟）。
        这样做的原因是：游戏可能在闭关结束后几秒到几十秒后才允许结算，
        提前检查会得到"还在闭关中"的回复，导致无谓的循环。

        参数:
            end_time: 闭关结束时间字符串
            label: 日志标签
        """
        protected_until = self.meditation_protected_until({"deep_meditation_end_time": end_time}) or end_time
        remaining = seconds_until(protected_until)
        if remaining <= 0:
            return

        wait_sec = remaining + random.randint(10, 30)
        if wait_sec > 0:
            log.info(f"{label}: waiting {wait_sec:.0f}s for deep meditation settlement window.")
            try:
                await asyncio.wait_for(
                    self.meditation_state_event.wait(),
                    timeout=scheduler_sleep_seconds(wait_sec),
                )
                self.meditation_state_event.clear()
                log.info(f"{label}: meditation state changed, rechecking.")
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # 问心台策略
    # ------------------------------------------------------------------

    async def maybe_use_heart_platform_before_climb(self, curr_step, today, allow_daily_fallback=False):
        """
        登天阶前判断是否使用问心台 buff。

        使用条件：
          1. 当前阶数在 8-11 阶之间（高阶登阶需要 buff 辅助）
             或者当天保底时间到了（allow_daily_fallback）
          2. 问心台不在冷却中
          3. 今天还没用过问心台
          4. 没有未使用的罡风 buff（罡风优先级更高）

        为什么罡风优先？
        因为罡风 buff 持续时间更长（12 小时 CD），且效果可能更强，
        如果有罡风 buff 未用，应该先用罡风登阶，而不是用问心台覆盖掉。

        参数:
            curr_step: 当前云阶步数
            today: 日期字符串
            allow_daily_fallback: 是否允许每日保底触发
        """
        late_fallback = allow_daily_fallback and self.is_heart_platform_fallback_due(today)
        # 非高阶（8-11）且非保底时间，跳过
        if not (8 <= curr_step <= 11) and not late_fallback:
            return
        # 冷却中，跳过
        if self.is_heart_platform_throttled():
            return
        # 今天已使用，跳过
        if self.heart_platform_already_used_today(today):
            return

        # 罡风 buff 还在的话，问心台让路
        wind_pending = self.has_pending_wind_buff()
        if wind_pending:
            log.info("Heart Platform skipped: pending Wind buff has priority.")
            return

        # 如果罡风冷却完毕但还没用，优先用罡风
        if self.is_nine_heaven_wind_ready():
            if self.dashboard_command_paused(".引九天罡风", "主魂"):
                log.info("Heart Platform precheck: .引九天罡风 is paused by dashboard; skipping Wind send.")
            else:
                log.info(f"Heart Platform check at {curr_step}/12, but Wind is ready. Sending .引九天罡风 first; Heart Platform is skipped.")
                wind_resp = await self.send_and_wait_feedback(".引九天罡风", timeout=120, force_identity_check=True)
                wind_pending = self.record_nine_heaven_wind_response(wind_resp, source="Cloud Stairs")
                await asyncio.sleep(3)

                # 如果罡风用了或者还是冷却完毕状态（说明施展失败），跳过问心台
                if wind_pending or self.is_nine_heaven_wind_ready():
                    log.info("Heart Platform skipped to preserve Wind priority.")
                    return

        if self.dashboard_command_paused(".问心台", "主魂"):
            log.info("Heart Platform skipped: .问心台 is paused by dashboard.")
            return

        # 使用问心台
        reason = "late daily fallback" if late_fallback and not (8 <= curr_step <= 11) else "late cloud-stairs climb"
        log.info(f"Progress {curr_step}/12, Wind unavailable, sending .问心台 for {reason}.")

        hp_resp = await self.send_and_wait_feedback(".问心台", force_identity_check=True)
        if hp_resp:
            if self.is_lingxiao_identity_mismatch_response(hp_resp):
                self._main_confirmed = False
                self.state["next_heart_time"] = add_seconds_str(now_str(), 120)
                self.save_state()
                log.warning("Heart Platform: main identity mismatch; will force .切换 主魂 before retry.")
                return
            if any(k in hp_resp for k in ["问心台", "已经", "明天", "成功", "感受到", "感悟", "今日"]):
                self.state["heart_platform_date"] = today
                self.state["last_heart_time"] = now_str()
                self.state["next_heart_time"] = add_seconds_str(f"{today} 00:05:00", 24 * 3600)
                self.save_state()
                log.info("Heart Platform used/confirmed for late cloud-stairs climb.")
            else:
                log.warning(f"Heart Platform response unusual: {hp_resp[:100]}")
                notify_unrecognized_response(self, ".问心台", hp_resp, log, "问心台")

    # ------------------------------------------------------------------
    # 主循环身份守卫
    # ------------------------------------------------------------------

    async def _wait_for_main_identity(self):
        """
        主循环身份守卫：只等待正在发送的化身指令完成。
        不在这里主动切回主魂；真正要发送主魂指令时由 send_and_wait_feedback 对齐身份。
        """
        while getattr(self, "is_running", True):
            remaining = self.identity_pause_seconds("主魂")
            if remaining <= 0:
                break
            await asyncio.sleep(max(30, min(int(remaining), 300)))
        while self.avatar_send_lock.locked():
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # 九天罡风循环
    # ------------------------------------------------------------------

    async def run_nine_heaven_wind_loop(self):
        """
        引九天罡风循环。
        独立的后台任务，每隔一段时间检查罡风是否冷却完毕，可施展则施展。

        循环逻辑：
          1. 检查是否冷却完毕（is_nine_heaven_wind_ready）
          2. 如果有未使用的问心台 buff 且罡风可用，等待一会儿再试
             （给登天阶循环优先消耗问心台 buff 的机会）
          3. 施展罡风
          4. 根据 CD 设置等待时间

        为什么这是个独立循环而不是放在登天阶循环里？
        因为罡风 CD 12 小时，和登天阶 CD 3 小时不同步，
        独立循环可以各自按自己的节奏运行，互不干扰。
        """
        await self.startup_done.wait()  # 等待启动同步完成
        while self.is_running:
            await self._wait_for_main_identity()
            if self.dashboard_command_paused(".引九天罡风", "主魂"):
                log.info("Nine Heaven Wind is paused by dashboard; waiting before recheck.")
                await self.wait_for_dashboard_command_control_change(scheduler_sleep_seconds(600))
                continue

            round_locked = self.defer_nine_heaven_wind_if_round_locked("Nine Heaven Wind state")
            should_use_wind = False if round_locked else self.is_nine_heaven_wind_ready()
            heart_pending = self.has_pending_heart_buff()

            if should_use_wind and heart_pending:
                # 有问心台 buff 待用且罡风可用，短等 30 秒让登天阶循环先消耗问心台
                log.info("Nine Heaven Wind ready, but Heart Platform buff is pending for an immediate climb. Retrying shortly.")
            elif should_use_wind:
                log.info("Sending .引九天罡风...")
                wind_resp = await self.send_and_wait_feedback(".引九天罡风", timeout=120, force_identity_check=True)
                self.record_nine_heaven_wind_response(wind_resp)

            # 计算睡眠时间
            next_wind_str = self.state.get("nine_heaven_wind_cd_time", 0)
            wait_time = 300  # 默认 5 分钟检查一次

            if should_use_wind and heart_pending:
                wait_time = 30  # 短等待，给登天阶循环机会
            elif isinstance(next_wind_str, str) and is_future(next_wind_str):
                wait_time = seconds_until(next_wind_str)  # 精确等待到冷却结束

            variance = random.randint(10, 30)  # 随机抖动，防检测
            log.info(f"Nine Heaven Wind Loop Complete. Sleep {wait_time + variance}s.")
            await asyncio.sleep(scheduler_sleep_seconds(wait_time + variance))

    async def stop_for_rift_weakness(self, response, identity="主魂", msg=None):
        """
        检测到元婴虚弱期时只暂停触发身份：
          1. 记录身份级暂停时间，防止该身份继续发指令
          2. 主魂触发时同步推迟下次探寻裂缝
          3. 发送告警通知用户，其他身份继续执行

        参数:
            response: 触发停止的回复文本（附带在告警中供用户参考）
        """
        identity = str(identity or "").strip() or "主魂"
        pause_until = self.mark_identity_rift_rebirth_pending(identity, response, source=".探寻裂缝")
        await send_text_alert(
            self,
            "凌霄宫探寻裂缝告警",
            f"探寻裂缝触发元婴虚弱期，主号身份【{identity}】已暂停；其他身份继续执行。\n\n"
            f"恢复条件：发送 `.重生 1` / `.重生 2` / `.重生 3` 任一成功后自动恢复。\n"
            f"当前状态：{pause_until}\n\n机器人回复：\n{response}",
            log,
        )
        log.critical(f"Rift weakness detected. Identity [{identity}] paused until rebirth succeeds; other identities continue.\n{response}")

    # ------------------------------------------------------------------
    # 元婴出窍循环
    # ------------------------------------------------------------------

    async def run_yuanying_out_loop(self):
        """
        元婴出窍循环。
        管理元婴出窍和归窍的完整生命周期。

        循环逻辑：
          1. 如果元婴在外活跃且归窍时间未到
             -> 等待到归窍时间（最多 600 秒检查一次）
          2. 如果元婴在外但归窍时间已到
             -> 发 自动归窍，然后等待 5 秒进入下一步
          3. 如果元婴不在外但 CD 未到
             -> 等待 CD 结束
          4. 如果元婴不在外且 CD 已到
             -> 发 .元婴出窍，解析回复更新状态

        为什么不直接把归窍和出窍写在一起？
        因为归窍后可以立即再出窍（不占用归窍时间），
        所以归窍成功后直接 fall through 到出窍判断。
        """
        await self.startup_done.wait()
        while self.is_running:
            wait_time = await self.common_main_yuanying_out_tick()
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ------------------------------------------------------------------
    # 探寻裂缝循环
    # ------------------------------------------------------------------

    async def run_rift_search_loop(self):
        """
        探寻裂缝循环。
        定时发送 .探寻裂缝 指令，并处理回复。

        特殊处理：如果检测到元婴虚弱期，
        调用 stop_for_rift_weakness 紧急停止整个脚本。

        验证机制：对虚弱期回复做 msg_id 验证，
        确保虚弱期提示确实是对我们指令的回复，
        而不是其他人触发的。
        """
        await self.startup_done.wait()

        while self.is_running:
            wait_time = await self.common_main_rift_search_tick(RIFT_SEARCH_CD_SECONDS)
            if wait_time < 0:
                break
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ------------------------------------------------------------------
    # 抚摸法宝循环
    # ------------------------------------------------------------------

    async def run_treasure_touch_loop(self):
        """
        定时抚摸本命法宝器灵循环。
        定时（默认 2 小时冷却）发送抚摸法宝指令，
        提升法宝与主人的亲密度/默契度。
        仅在主魂身份时执行，化身身份时跳过等待。
        """
        return await self.run_common_treasure_touch_loop(
            TREASURE_TOUCH_COMMAND,
            sleep_func=scheduler_sleep_seconds,
        )


    def record_nurture_spirit_response(self, resp):
        """解析温养器灵的回复并更新状态（6小时冷却）"""
        plan = self.nurture_spirit_plan(NURTURE_SPIRIT_COMMAND)
        command = plan.command
        next_key = plan.next_key
        last_key = plan.last_key
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 3600)
            log.info(f"{command}: response missing; conservative retry at {self.state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{command}: cooldown {cd}s, next at {self.state[next_key]}.")
            return False

        if any(k in resp for k in ["灵石", "养魂木", "材料", "不足", "不够", "没有", "无法", "错误"]):
            now = now_str()
            self.state[next_key] = add_seconds_str(now, 6 * 3600)
            log.info(f"{command}: blocked by resource/validation response; next check at {self.state[next_key]}.")
            return False

        if any(k in resp for k in ["温养", "默契", "经验", "成功", "提升", "喜悦"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, 6 * 3600)
            log.info(f"{command}: success, next at {self.state[next_key]}.")
            return True

        notify_unrecognized_response(self, command, resp, log, "温养器灵")
        self.state[next_key] = add_seconds_str(now_str(), 3600)
        log.info(f"{command}: unrecognized; conservative retry at {self.state[next_key]}.")
        return False

    
    # ------------------------------------------------------------------
    # 温养器灵循环
    # ------------------------------------------------------------------

    async def run_nurture_spirit_loop(self):
        """定时温养器灵循环（6小时冷却）"""
        await self.startup_done.wait()
        plan = self.nurture_spirit_plan(NURTURE_SPIRIT_COMMAND)
        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get(plan.next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Nurture spirit loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(scheduler_sleep_seconds(wait_time))
                continue

            if self.dashboard_command_paused(plan.command, "主魂"):
                log.info(f"Nurture spirit paused by dashboard: {plan.command}.")
                if not await self.wait_for_dashboard_command_control_change(300):
                    await asyncio.sleep(300)
                continue

            log.info(f"Nurture spirit due: sending {plan.command}.")
            resp = await self.send_and_wait_feedback(
                plan.command,
                timeout=plan.timeout,
                max_retries=plan.max_retries,
                force_identity_check=plan.force_identity_check,
            )
            self.record_nurture_spirit_response(resp)
            self.save_state()
            wait_time = seconds_until(self.state.get(plan.next_key, "")) or 600
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

        # ------------------------------------------------------------------
    # 登天阶循环（主循环之一）
    # ------------------------------------------------------------------

    async def run_cloud_stairs_loop(self):
        """
        凌霄宫云阶问心循环。
        这是最主要的玩法循环之一，管理登天阶和问心台的使用策略。

        循环逻辑：
          1. 登天阶执行（CD 到了就登）
             - 登阶前判断是否需要先用问心台
          2. 问心台每日保底（23:50 强制使用）
          3. 计算等待时间

        问心台使用策略（在 maybe_use_heart_platform_before_climb 中实现）：
          - 高阶（8-11 阶）优先用问心台辅助
          - 罡风 buff 优先于问心台 buff
          - 23:50 保底使用
        """
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            stairs_paused = self.dashboard_command_paused(".登天阶", "主魂")
            heart_paused = self.dashboard_command_paused(".问心台", "主魂")

            next_stairs = self.restore_cloud_stairs_time_from_last()
            if next_stairs and is_future(next_stairs):
                log.info(f"Cloud stairs CD active. Skipping .天阶状态. Next Stairs: {next_stairs}")
            elif not self.state.get("cloud_stairs_progress"):
                log.info("Cloud stairs progress missing. Skipping .天阶状态 before climb; relying on .登天阶 response.")
            else:
                log.info(f"Cloud stairs ready by local cache. Skipping .天阶状态. Next Stairs: {next_stairs}")

            # ---- 1. 登天阶执行 ----
            next_time_str = self.state.get("next_stairs_time", "")
            curr_step = self.get_cloud_stairs_step()
            today = datetime.now().strftime('%Y-%m-%d')

            if not next_time_str or not is_future(next_time_str):
                if stairs_paused:
                    log.info("Cloud stairs climb skipped: .登天阶 is paused by dashboard.")
                    if await self.wait_for_dashboard_command_control_change(scheduler_sleep_seconds(600)):
                        log.info("Cloud stairs command controls changed; rechecking now.")
                    continue

                # 登阶前先考虑是否用问心台
                await self.maybe_use_heart_platform_before_climb(curr_step, today)

                pre_send_next = self.restore_cloud_stairs_time_from_last()
                if pre_send_next and is_future(pre_send_next):
                    log.info(f"Cloud stairs send suppressed by refreshed CD: {pre_send_next}")
                else:
                    log.info("Sending .登天阶...")
                    stairs_resp = await self.send_and_wait_feedback(".登天阶", timeout=120, force_identity_check=True)
                    if stairs_resp:
                        self.record_cloud_stairs_response(stairs_resp)
                        self.save_state()
                    else:
                        self.state["next_stairs_time"] = add_seconds_str(now_str(), 120)
                        log.warning(f"Cloud stairs response missing; delaying retry until {self.state['next_stairs_time']}.")
                        self.save_state()
            elif self.state.get("heart_platform_date") != today and self.is_heart_platform_fallback_due(today):
                # 登天阶 CD 中，但问心台保底时间到了
                if heart_paused:
                    log.info("Heart Platform daily fallback skipped: .问心台 is paused by dashboard.")
                else:
                    await self.maybe_use_heart_platform_before_climb(curr_step, today, allow_daily_fallback=True)

            # ---- 2. 计算等待时间 ----
            next_stairs_str = self.state.get("next_stairs_time", "")
            wait_time = random.randint(10, 20)  # 如果 CD 到了，默认只睡一小会儿

            if next_stairs_str and is_future(next_stairs_str):
                wait_time = seconds_until(next_stairs_str) + random.randint(5, 15)
                log.info(f"Stairs CD active. Sleeping {wait_time}s until {next_stairs_str}")
            else:
                log.info(f"Stairs ready or no CD. Short sleep {wait_time}s before next attempt.")
                log.info(f"Cloud stairs next: {next_stairs_str}")

            # ---- 3. 问心台每日保底调度 ----
            # 如果今天还没用问心台，检查是否需要提前醒来执行保底
            if self.state.get("heart_platform_date") != today and not heart_paused:
                fallback_time = self.heart_platform_fallback_time(today)
                if is_future(fallback_time):
                    heart_wait = seconds_until(fallback_time) + random.randint(5, 15)
                    if heart_wait < wait_time:
                        wait_time = heart_wait
                        self.state["next_heart_time"] = fallback_time
                        self.save_state()
                        log.info(f"Heart Platform daily fallback pending. Sleeping {wait_time}s until {fallback_time}")

            variance = random.randint(10, 30)
            log.info(f"Cloud Stairs Loop Complete. Sleep {wait_time + variance}s.")
            await asyncio.sleep(scheduler_sleep_seconds(wait_time + variance))

    # ------------------------------------------------------------------
    # 每日任务循环
    # ------------------------------------------------------------------

    async def run_daily_tasks(self):
        """
        每日任务循环。
        每天早上 7:00 执行一次，完成以下任务：
          1. 宗门点卯（.宗门点卯）
          2. 闯塔（.闯塔，配合 .借天门势）

        实现细节：
          - 使用 state["done"] 记录今日已完成的任务，防止重复执行
          - 闯塔前先 .借天门势（增加成功率）
        """
        return await self.run_common_daily_tasks_loop(
            seconds_until_daily_task_start,
            daily_task_start_label,
            [".宗门点卯", ".闯塔"],
            pre_loop_func=self._wait_for_main_identity,
            sleep_func=scheduler_sleep_seconds,
            send_kwargs_func=lambda command: {"return_msg": True},
            mark_done_before_send=True,
            use_lingxiao_tower_buff=True,
            reset_heart_platform_date=True,
        )

    def _stale_fishing_active_identities(self, overdue_seconds=60):
        stale = []
        for identity in ["主魂", *list(getattr(self, "avatars", []) or [])]:
            try:
                state = self.get_fishing_state(identity)
            except Exception:
                continue
            due_at = str(state.get("active_due_at") or "").strip()
            if not state.get("active") or not due_at or is_future(due_at):
                continue
            overdue = seconds_until(due_at)
            if overdue <= -abs(int(overdue_seconds)):
                stale.append((identity, due_at, int(abs(overdue))))
        return stale

    async def run_health_watchdog_loop(self):
        await self.startup_done.wait()
        lock_started_at = None
        while getattr(self, "is_running", True):
            try:
                stale_fishing = self._stale_fishing_active_identities()
                if stale_fishing:
                    detail = ", ".join(
                        f"{identity} due {due_at} ({overdue}s overdue)"
                        for identity, due_at, overdue in stale_fishing
                    )
                    log.critical(f"Main watchdog: stale fishing active detected: {detail}; restarting process.")
                    self.save_state()
                    os.execv(sys.executable, [sys.executable, *sys.argv])

                if self.avatar_send_lock.locked():
                    if lock_started_at is None:
                        lock_started_at = time.monotonic()
                    held_for = time.monotonic() - lock_started_at
                    if held_for >= 10 * 60:
                        log.critical(f"Main watchdog: avatar_send_lock held for {held_for:.0f}s; restarting process.")
                        self.save_state()
                        os.execv(sys.executable, [sys.executable, *sys.argv])
                else:
                    lock_started_at = None
            except Exception as exc:
                log.error(f"Main watchdog loop error: {exc}", exc_info=True)
            await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # 闭关循环（核心玩法之一）
    # ------------------------------------------------------------------

    async def run_meditation_timer(self):
        """
        统一闭关逻辑（根据 5 条新规制）。

        闭关是游戏的核心成长机制，角色闭关修炼 8 小时获得大量修为。
        本函数实现了完整的闭关生命周期管理：

        步骤图解：
          Step 1: 发送 .深度闭关 -> 获取闭关时长（通常 8 小时）
          Step 2: 成功开启 -> 睡足 8 小时
          Step 3: 发送 .查看闭关 -> 检查状态
            - 如果还在闭关（有时间返回）-> 继续等到时间结束
            - 如果已到期（神魂归位/未在闭关）-> 去 Step 4
          Step 4: 发送 .闭关修炼 -> 结算闭关收益
          Step 5: 回到 Step 1（重新开启新一轮闭关）

        异常处理：
          - 如果 .深度闭关 被拦截（冷却中、无法闭关等）
            -> 通过 .闭关修炼 保底激活
          - 如果回复无法识别
            -> 10 分钟后重试
          - 启动时检查是否已在深度闭关中（恢复状态）
        """
        await self.startup_done.wait()

        # 启动恢复：如果上次退出时正在深度闭关中，恢复状态
        retry_time = self.meditation_defer_until(self.state)
        if retry_time and is_future(retry_time):
            log.info(f"Meditation: deferred until {retry_time}.")
            await asyncio.sleep(scheduler_sleep_seconds(seconds_until(retry_time)))
            self.state["next_meditation_retry_time"] = ""
            self.save_state()

        # === 启动恢复：检查是否处于深度闭关中 ===
        if self.state.get("in_deep_meditation"):
            end_time = self.state.get("deep_meditation_end_time", "")
            if end_time and is_future(end_time):
                await self.sleep_until_meditation_check(end_time, label="Startup recovery")
            else:
                log.info("Startup recovery: 8h passed, entering checking phase.")

        async def sync_deep_meditation_start(med_text, source):
            """Record a successful/ongoing deep meditation response and verify time if needed."""
            return await self.sync_main_deep_meditation_start(med_text, source)

        async def settle_and_restart_meditation(reason):
            """Settle completed meditation and immediately start the next deep meditation."""
            log.info(f"Meditation Step 4: {reason}. Settling and restarting immediately...")
            cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
            if self.defer_meditation_after_cultivation_cooldown(
                "主魂", cultivation_resp, "Meditation Step 4 .闭关修炼"
            ):
                return
            await asyncio.sleep(3)
            med_resp = await self.send_and_wait_feedback(".深度闭关")
            med_text = getattr(med_resp, "text", "") if hasattr(med_resp, "text") else med_resp if isinstance(med_resp, str) else str(med_resp) if med_resp else ""
            if await sync_deep_meditation_start(med_text, ".深度闭关 restart"):
                await self.place_concubine_after_meditation_start()
                return
            cd = self.parse_wait_time(med_text)
            retry_cd = cd if cd > 0 else 600
            if med_text:
                notify_unrecognized_response(self, ".深度闭关", med_text, log, "深度闭关")
            self.state["in_deep_meditation"] = False
            self.state["deep_meditation_end_time"] = ""
            self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), retry_cd)
            self.save_state()

        while self.is_running:
            await self._wait_for_main_identity()
            # 检查重试延迟
            retry_time = self.meditation_defer_until(self.state)
            if retry_time and is_future(retry_time):
                log.info(f"Meditation: deferred until {retry_time}.")
                await asyncio.sleep(scheduler_sleep_seconds(seconds_until(retry_time)))
                self.state["next_meditation_retry_time"] = ""
                self.save_state()
                continue

            # 如果当前状态已经是"深度闭关中"，直接跳到监控步骤（Step 3）
            if not self.state.get("in_deep_meditation"):
                # === Step 1: 发送 .深度闭关 ===
                log.info("Meditation Step 1: Sending .深度闭关...")
                resp = await self.send_and_wait_feedback(".深度闭关")
                resp = resp or ""

                # 识别到"已在"或开启成功，进入闭关监控状态
                if await sync_deep_meditation_start(resp, ".深度闭关 start"):
                    end_time = self.state.get("deep_meditation_end_time", "")
                    if end_time and is_future(end_time):
                        # Step 2: 开启成功，睡足 8 小时
                        await self.place_concubine_after_meditation_start()
                        log.info(f"Meditation Step 2: Started/active, end at {end_time}.")
                        await self.sleep_until_meditation_check(end_time)
                    else:
                        log.info("Meditation: active but end time missing, moving to Step 3.")
                else:
                    # Step 1 失败：被游戏拒绝
                    if resp and any(k in resp for k in ["冷却", "后再", "无法", "尚未", "普通闭关", "闭关修炼", "先闭关"]):
                        # 启动被明确拦截 -> 通过 .闭关修炼 保底激活
                        log.info("Meditation: Start blocked, activating via .闭关修炼...")
                        cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
                        self.defer_meditation_after_cultivation_cooldown(
                            "主魂", cultivation_resp, "Meditation start fallback .闭关修炼"
                        )
                        await asyncio.sleep(3)
                        med_resp = await self.send_and_wait_feedback(".深度闭关")
                        med_text = getattr(med_resp, "text", "") if hasattr(med_resp, "text") else med_resp if isinstance(med_resp, str) else str(med_resp) if med_resp else ""
                        if await sync_deep_meditation_start(med_text, ".深度闭关 start fallback"):
                            end_time = self.state.get("deep_meditation_end_time", "")
                            if end_time and is_future(end_time):
                                await self.place_concubine_after_meditation_start()
                                await self.sleep_until_meditation_check(end_time)
                        else:
                            cd = self.parse_wait_time(med_text)
                            retry_cd = cd if cd > 0 else 600
                            if med_text:
                                notify_unrecognized_response(self, ".深度闭关", med_text, log, "深度闭关")
                            self.state["in_deep_meditation"] = False
                            self.state["deep_meditation_end_time"] = ""
                            self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), retry_cd)
                            self.save_state()
                    else:
                        # 无法识别的回复
                        if resp:
                            notify_unrecognized_response(self, ".深度闭关", resp, log, "深度闭关")
                        self.state["in_deep_meditation"] = False
                        self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600)
                        self.save_state()
                        await asyncio.sleep(scheduler_sleep_seconds(600))
                    continue

            # === Step 3 & 4: 监控与结算 ===
            while self.is_running and self.state.get("in_deep_meditation"):
                if self.ensure_meditation_guard_from_end_time(self.state):
                    self.save_state()
                guard_wait = self.meditation_guard_wait_seconds_for_state(self.state)
                if guard_wait > 0:
                    log.info(
                        f"Meditation Step 3: protected from .查看闭关 for "
                        f"{self.compact_duration_text(guard_wait)}."
                    )
                    await asyncio.sleep(scheduler_sleep_seconds(guard_wait + random.randint(10, 30)))
                    continue

                end_time = self.state.get("deep_meditation_end_time", "")
                if end_time and is_future(end_time):
                    # 本地状态显示还在闭关，等待到结束时间
                    log.info(f"Meditation Step 3: Local state valid (ends at {end_time}).")
                    await self.sleep_until_meditation_check(end_time)
                    continue

                # 本地时间已过期，发 .查看闭关 确认实际状态
                log.info("Meditation Step 3: Time expired or missing. Sending .查看闭关...")
                check_resp = await self.send_and_wait_feedback(".查看闭关")
                cd_check = self.parse_wait_time(check_resp)

                if cd_check > 0:
                    # 确实还在闭关，更新结束时间继续等待
                    self.state.update(
                        self.meditation_active_state_values(
                            add_seconds_str(now_str(), cd_check),
                            clear_restart=False,
                        )
                    )
                    self.save_state()
                    log.info(f"Meditation: Still in progress, remaining {cd_check}s.")
                    await self.sleep_until_meditation_check(self.state["deep_meditation_end_time"])
                elif is_deep_meditation_settlement_response(check_resp):
                    # Step 4: 神魂归位/总结：闭关已到期，需立即结算并重开
                    await settle_and_restart_meditation("Session completed")
                    continue
                elif is_not_deep_meditation_response(check_resp):
                    # 明确未在闭关 -> 结算并重开
                    await settle_and_restart_meditation("Confirmed NOT in meditation")
                    continue
                elif is_deep_meditation_ongoing_response(check_resp):
                    # 确实在闭关，只是没给时间
                    log.info("Meditation: Ongoing but no time info. Waiting 10 minutes for sync.")
                    await asyncio.sleep(scheduler_sleep_seconds(600))
                else:
                    # 未知状态
                    log.warning("Meditation Step 4: Unknown status. Skipping reset and retrying later.")
                    notify_unrecognized_response(self, ".查看闭关", check_resp, log, "闭关状态")
                    self.state["in_deep_meditation"] = False
                    self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600)
                    self.save_state()
                    break

    # ------------------------------------------------------------------
    # 主循环（launcher）
    # ------------------------------------------------------------------


    # ============================================================
    # 身外化身：辅助检测方法（从星宫迁移）
    # ============================================================

    def is_deep_meditation_start_success(self, text):
        """检测深度闭关是否成功开启"""
        if not text:
            return False
        clean = text.replace("**", "")
        return (
            any(k in clean for k in ["成功", "开启", "已进入深度闭关", "深度闭关状态", "神魂将自行吐纳"])
            or ("已在" in clean and "深度闭关" in clean)
        )

    async def sync_main_deep_meditation_start(self, med_text, source):
        """Record a main-soul deep meditation start/active response."""
        med_text = med_text or ""
        cd = self.parse_wait_time(med_text)
        already_active = "已在" in med_text and "深度闭关" in med_text
        start_success = self.is_deep_meditation_start_success(med_text)
        if cd <= 0 and already_active:
            verify_resp = await self.send_and_wait_feedback(".查看闭关")
            verify_text = (
                getattr(verify_resp, "text", "")
                if hasattr(verify_resp, "text")
                else verify_resp if isinstance(verify_resp, str)
                else str(verify_resp) if verify_resp else ""
            )
            verify_cd = self.parse_wait_time(verify_text)
            if verify_cd > 0 and is_deep_meditation_ongoing_response(verify_text):
                cd = verify_cd

        if cd <= 0 and not start_success:
            return False

        if cd > 0:
            end_time = add_seconds_str(now_str(), cd)
        elif already_active:
            # "已在深度闭关之中" carries no timer. Do not preserve a stale
            # local end time; let the monitor query .查看闭关 again for the real CD.
            end_time = ""
        else:
            end_time = add_seconds_str(now_str(), 8 * 3600)

        self.state.update(self.meditation_active_state_values(end_time, clear_restart=False))
        if not already_active:
            self.state["last_deep_meditation_time"] = now_str()
            self.state["last_deep_date"] = datetime.now().strftime("%Y-%m-%d")
        self.save_state()
        log.info(f"Meditation: active synced from {source}, end at {end_time}.")
        return True

    def message_effective_time_str(self, msg):
        """获取消息的有效时间（编辑时间优先）"""
        msg_dt = getattr(msg, "edit_date", None) or getattr(msg, "date", None)
        if not msg_dt:
            return now_str()
        try:
            return dt_to_str(datetime.fromtimestamp(msg_dt.timestamp()))
        except Exception:
            return now_str()

    def message_age_seconds(self, msg):
        """计算消息的年龄（秒）"""
        return CommonCommandMixin.message_age_seconds(self, msg)

    async def get_updated_message(self, msg, delay_sec=120):
        """等待后重新获取消息编辑后内容"""
        if not msg:
            return None
        await asyncio.sleep(delay_sec)
        try:
            return await self.client.get_messages(self.target_chat_id, ids=msg.id)
        except Exception:
            return msg

    # ============================================================
    # 身外化身：状态管理
    # ============================================================

    def ensure_avatar_states(self):
        """确保 state 中存在 avatars 化身专属状态区"""
        if "avatars" not in self.state:
            self.state["avatars"] = {}
        avatar_default = {
            "next_meditation_time": "", "last_meditation_time": "", "level": "",
            "next_field_training_time": "", "last_field_training_time": "",
            "bushi_wentian_date": "", "bushi_wentian_count": 0, "bushi_wentian_exchange_count": 0,
            "bushi_wentian_kunwu_exchanged": False,
            "nickname": "", "in_deep_meditation": False,
            "deep_meditation_end_time": "", "deep_meditation_guard_until": "",
            "meditation_restart_pending": False,
            "meditation_restart_mode": "", "last_tower_date": "",
            "next_dream_map_time": "", "next_heart_trial_time": "", "next_divination_time": "",
            "next_concubine_voyage_time": "", "last_concubine_voyage_time": "",
            "concubine_voyage_active": False,
            "last_yuanying_out_time": "", "last_yuanying_return_time": "",
            "next_yuanying_out_time": "", "yuanying_out_active": False,
            "yuanying_out_end_time": "", "last_rift_search_time": "",
            "next_rift_search_time": "",
            "last_destiny_date": "", "last_destiny_time": "", "last_destiny_choice": "",
            "last_star_attraction_time": "", "next_star_attraction_time": "",
            "last_star_appease_time": "", "next_star_appease_time": "",
            "last_star_collect_time": "", "next_star_collect_time": "",
            "last_star_observatory_time": "", "next_star_check_time": "",
            "star_observatory_summary": "", "star_observatory_needs_refresh": True,
            "star_attraction_retry_time": "", "star_attraction_force_exit_tried": False,
            "star_pre_collect_appeased_for": "", "star_target": STAR_ATTRACTION_TARGET,
            "current_exp": 0, "total_exp": 0, "spirit_root": "", "last_dianmao_date": "",
        }
        nicknames = dict(self.avatar_nicknames)
        changed = False
        for name in self.avatars:
            if name not in self.state["avatars"]:
                self.state["avatars"][name] = dict(avatar_default)
                self.state["avatars"][name]["nickname"] = nicknames.get(name, "")
                changed = True
            else:
                for k, v in avatar_default.items():
                    if k not in self.state["avatars"][name]:
                        self.state["avatars"][name][k] = v
                        changed = True
                if not self.state["avatars"][name].get("nickname"):
                    self.state["avatars"][name]["nickname"] = nicknames.get(name, "")
                    changed = True
        if changed:
            self.save_state()

    def get_avatar_state(self, avatar):
        self.ensure_avatar_states()
        return self.state["avatars"].get(avatar, {})

    def set_avatar_state(self, avatar, key, value):
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar][key] = value
            self.save_state()

    def update_avatar_states(self, avatar, values):
        self.ensure_avatar_states()
        if avatar not in self.state["avatars"]:
            return
        self.state["avatars"][avatar].update(values or {})
        self.save_state()

    def mark_avatar_meditation_restart_pending(self, avatar, source=""):
        """Mark an avatar as out of deep meditation and needing an immediate restart."""
        a_state = self.get_avatar_state(avatar)
        self.ensure_meditation_guard_from_end_time(a_state)
        a_state["in_deep_meditation"] = False
        a_state["deep_meditation_end_time"] = ""
        if source != "passive settlement":
            a_state["deep_meditation_guard_until"] = ""
        a_state["meditation_restart_pending"] = True
        source_text = str(source or "")
        if "force" in source_text.lower() or "强行" in source_text:
            a_state["meditation_restart_mode"] = "deep_only"
        elif not a_state.get("meditation_restart_mode"):
            a_state["meditation_restart_mode"] = ""
        self.save_state()
        log.info(f"[{avatar}] meditation restart pending ({source}).")

    def avatar_meditation_needs_attention(self, avatar):
        a_state = self.get_avatar_state(avatar)
        next_med = self.meditation_defer_until(a_state)
        if next_med and is_future(next_med):
            return False
        if a_state.get("meditation_restart_pending"):
            return True
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            return not end_time or not is_future(end_time)
        return not a_state.get("deep_meditation_end_time")

        # 以下是被动状态检测方法（从万灵宗移植）
        # 用于监控手动发送指令的结果，及时同步到 state

    def clean_spirit_tree_text(self, text):
        return str(text or "").replace("**", "").strip()

    def spirit_tree_text_is_guide_text(self, text):
        clean = self.clean_spirit_tree_text(text)
        return bool(
            clean
            and (
                "灵眼之树要诀" in clean
                or ("采摘期开启后" in clean and ".采摘灵果" in clean)
                or ("采摘期开启后" in clean and "采摘灵果" in clean)
            )
        )

    def spirit_tree_text_indicates_mature(self, text):
        clean = self.clean_spirit_tree_text(text)
        if self.spirit_tree_text_is_guide_text(clean):
            return False
        if any(k in clean for k in SPIRIT_TREE_MATURE_KEYWORDS):
            return True
        if "灵眼之树已然成熟" in clean:
            return True
        if "落云宗" in clean and "灵眼之树" in clean and "状态" in clean and "成熟采摘期" in clean:
            return True
        return (
            "采摘期" in clean
            and any(k in clean for k in ["剩余", "结束", "已开启", "开启中", "可采摘"])
            and "可查看神树成熟进度" not in clean
            and not self.spirit_tree_text_is_guide_text(clean)
        )

    def spirit_tree_text_is_global_state(self, text):
        clean = self.clean_spirit_tree_text(text)
        return (
            "落云宗" in clean
            and "灵眼之树" in clean
            and any(k in clean for k in [
                "状态", "成熟采摘期", "成熟度", "剩余", "当前玩法",
                "进度", "警报", "三派异动", "护山底蕴", "守山次数",
            ])
        )

    def spirit_tree_text_indicates_invasion(self, text):
        clean = self.clean_spirit_tree_text(text)
        if not clean or "古剑门" not in clean:
            return False
        if any(k in clean for k in ["当前并无外敌", "无需加固", "无需守山"]):
            return False
        if any(k in clean for k in ["暂息旧隙", "试剑修枝", "顺手替灵树斩去乱枝"]):
            return False
        if "三派异动" in clean and not any(k in clean for k in [
            "警报", "入侵中", "请速用", "大阵耐久",
        ]):
            return False
        return any(k in clean for k in [
            "古剑门来袭", "古剑门入侵", "古剑门入侵中",
            "请速用 `.协同守山`", "请速用 .协同守山", "大阵耐久",
        ])

    def spirit_tree_text_indicates_irrigation_state(self, text):
        clean = self.clean_spirit_tree_text(text)
        if not clean or self.spirit_tree_text_indicates_mature(clean):
            return False
        if "灵树灌溉" in clean and "成熟度" in clean:
            return True
        return (
            "灵眼之树" in clean
            and "进度" in clean
            and any(k in clean for k in ["阶段", "环境", "成熟度"])
        )

    def spirit_tree_text_is_irrigation_cooldown(self, text):
        clean = self.clean_spirit_tree_text(text)
        return bool(
            clean
            and "灌溉" in clean
            and any(k in clean for k in ["地脉灵气尚未恢复", "后再来灌溉", "灌溉冷却", "尚未恢复"])
        )

    def spirit_tree_text_is_no_irrigation_response(self, text):
        clean = self.clean_spirit_tree_text(text)
        return (
            "无需灌溉" in clean
            and ("已然成熟" in clean or "正遭劫难" in clean or "遭劫难" in clean)
        )

    def parse_spirit_tree_mature_seconds(self, text):
        clean = self.clean_spirit_tree_text(text)
        if self.spirit_tree_text_is_no_irrigation_response(clean):
            return 0, False
        for line in clean.splitlines():
            if "采摘期" in line or "灵果已完全成熟" in line:
                cd = self.parse_wait_time(line)
                if cd > 0:
                    return cd, True
        cd = self.parse_wait_time(clean)
        if cd > 0 and any(k in clean for k in ["采摘期剩余", "采摘期结束", "成熟采摘期"]):
            return cd, True
        return SPIRIT_TREE_MATURE_SECONDS, False

    def spirit_tree_state_for_identity(self, identity=SPIRIT_TREE_AVATAR):
        # 灵眼之树是落云宗世界事件；成熟期/灌溉期/守山状态全账号共享。
        state = self.state
        changed = False
        for key, value in spirit_tree_default_state().items():
            if key not in state:
                state[key] = value
                changed = True
        if changed:
            self.save_state()
        return state

    def spirit_tree_identity_key(self, identity):
        identity = str(identity or "主魂").strip() or "主魂"
        return identity

    def spirit_tree_irrigation_times(self):
        state = self.spirit_tree_state_for_identity("主魂")
        changed = False
        times = state.get("spirit_tree_irrigation_times")
        if not isinstance(times, dict):
            times = {}
            state["spirit_tree_irrigation_times"] = times
            changed = True
        legacy_main = state.get("next_spirit_tree_irrigation_time", "")
        if legacy_main and not times.get("主魂"):
            times["主魂"] = legacy_main
            changed = True
        avatar_states = state.get("avatars", {}) if isinstance(state.get("avatars", {}), dict) else {}
        for avatar, a_state in avatar_states.items():
            if not isinstance(a_state, dict):
                continue
            legacy_avatar = a_state.get("next_spirit_tree_irrigation_time", "")
            if legacy_avatar and not times.get(avatar):
                times[avatar] = legacy_avatar
                changed = True
        if changed:
            self.save_state()
        return times

    def get_spirit_tree_irrigation_time(self, identity="主魂"):
        identity = self.spirit_tree_identity_key(identity)
        times = self.spirit_tree_irrigation_times()
        value = times.get(identity, "")
        if not value and identity == "主魂":
            value = self.state.get("next_spirit_tree_irrigation_time", "")
        return value or ""

    def set_spirit_tree_irrigation_time(self, identity="主魂", value=""):
        identity = self.spirit_tree_identity_key(identity)
        state = self.spirit_tree_state_for_identity(identity)
        times = self.spirit_tree_irrigation_times()
        if value:
            times[identity] = value
        else:
            times.pop(identity, None)
        if identity == "主魂":
            state["next_spirit_tree_irrigation_time"] = value or ""
        self.save_state()

    def spirit_tree_guard_times(self):
        state = self.spirit_tree_state_for_identity("主魂")
        changed = False
        times = state.get("spirit_tree_guard_times")
        if not isinstance(times, dict):
            times = {}
            state["spirit_tree_guard_times"] = times
            changed = True
        legacy_main = state.get("next_spirit_tree_guard_time", "")
        if legacy_main and not times.get("主魂"):
            times["主魂"] = legacy_main
            changed = True
        if changed:
            self.save_state()
        return times

    def spirit_tree_guard_last_times(self):
        state = self.spirit_tree_state_for_identity("主魂")
        changed = False
        times = state.get("spirit_tree_guard_last_times")
        if not isinstance(times, dict):
            times = {}
            state["spirit_tree_guard_last_times"] = times
            changed = True
        legacy_main = state.get("last_spirit_tree_guard_time", "")
        if legacy_main and not times.get("主魂"):
            times["主魂"] = legacy_main
            changed = True
        if changed:
            self.save_state()
        return times

    def get_spirit_tree_guard_time(self, identity="主魂"):
        identity = self.spirit_tree_identity_key(identity)
        times = self.spirit_tree_guard_times()
        value = times.get(identity, "")
        if not value and identity == "主魂":
            value = self.state.get("next_spirit_tree_guard_time", "")
        return value or ""

    def set_spirit_tree_guard_time(self, identity="主魂", value=""):
        identity = self.spirit_tree_identity_key(identity)
        state = self.spirit_tree_state_for_identity(identity)
        times = self.spirit_tree_guard_times()
        if value:
            times[identity] = value
        else:
            times.pop(identity, None)
        if identity == "主魂":
            state["next_spirit_tree_guard_time"] = value or ""
        self.save_state()

    def set_spirit_tree_last_guard_time(self, identity="主魂", value=""):
        identity = self.spirit_tree_identity_key(identity)
        state = self.spirit_tree_state_for_identity(identity)
        times = self.spirit_tree_guard_last_times()
        if value:
            times[identity] = value
        else:
            times.pop(identity, None)
        if identity == "主魂":
            state["last_spirit_tree_guard_time"] = value or ""
        self.save_state()

    def clear_spirit_tree_guard_times(self):
        state = self.spirit_tree_state_for_identity("主魂")
        state["spirit_tree_guard_times"] = {}
        state["next_spirit_tree_guard_time"] = ""
        self.save_state()

    def normalize_spirit_tree_state(self, identity=SPIRIT_TREE_AVATAR):
        a_state = self.spirit_tree_state_for_identity(identity)
        changed = False
        if not a_state.get("spirit_tree_status"):
            a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
            changed = True
        if a_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
            mature_until = a_state.get("spirit_tree_mature_until", "")
            if not mature_until:
                a_state["spirit_tree_mature_until"] = add_seconds_str(now_str(), SPIRIT_TREE_MATURE_SECONDS)
                mature_until = a_state["spirit_tree_mature_until"]
                changed = True
            if mature_until and not is_future(mature_until):
                a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
                a_state["spirit_tree_mature_until"] = ""
                a_state["spirit_tree_harvested_in_mature_period"] = False
                a_state["spirit_tree_harvest_attempted_in_mature_period"] = False
                a_state["spirit_tree_harvest_pending"] = False
                a_state["next_spirit_tree_irrigation_time"] = ""
                changed = True
            log.info(f"[{identity}] spirit tree mature period ended; resume irrigation.")
        if changed:
            self.save_state()
        return a_state

    def spirit_tree_harvest_lock_until(self, identity=SPIRIT_TREE_AVATAR):
        a_state = self.spirit_tree_state_for_identity(identity)
        value = a_state.get("next_spirit_tree_harvest_time", "")
        if value and is_future(value):
            return value
        if value:
            a_state["next_spirit_tree_harvest_time"] = ""
        return ""

    def spirit_tree_harvest_locked(self, identity=SPIRIT_TREE_AVATAR):
        return bool(self.spirit_tree_harvest_lock_until(identity))

    def record_spirit_tree_harvest_attempt(self, identity=SPIRIT_TREE_AVATAR, source=""):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        a_state = self.spirit_tree_state_for_identity(identity)
        now = now_str()
        next_time = add_seconds_str(now, SPIRIT_TREE_HARVEST_LOCK_SECONDS)
        a_state["spirit_tree_last_harvest_attempt_time"] = now
        a_state["next_spirit_tree_harvest_time"] = next_time
        a_state["spirit_tree_harvest_attempted_in_mature_period"] = True
        a_state["spirit_tree_harvest_pending"] = False
        self.save_state()
        log.info(f"[{identity}] spirit tree harvest attempt recorded ({source}); locked until {next_time}.")
        return next_time

    def record_spirit_tree_mature_state(self, text, msg=None, source="", identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        raw_state = self.spirit_tree_state_for_identity(identity)
        old_status = raw_state.get("spirit_tree_status", "")
        old_until = raw_state.get("spirit_tree_mature_until", "")
        old_harvested = bool(raw_state.get("spirit_tree_harvested_in_mature_period"))
        old_attempted = bool(raw_state.get("spirit_tree_harvest_attempted_in_mature_period"))
        a_state = self.normalize_spirit_tree_state(identity)
        seconds, parsed_from_status = self.parse_spirit_tree_mature_seconds(text)
        no_irrigation_only = self.spirit_tree_text_is_no_irrigation_response(text)
        msg_id = getattr(msg, "id", None) or 0
        same_msg = bool(msg_id and a_state.get("spirit_tree_last_mature_msg_id") == msg_id)
        already_mature = old_status == SPIRIT_TREE_MATURE_STATUS and old_until and is_future(old_until)
        recently_expired_mature = False
        if old_status == SPIRIT_TREE_MATURE_STATUS and old_until and not is_future(old_until):
            try:
                recently_expired_mature = 0 <= (datetime.now() - str_to_dt(old_until)).total_seconds() <= 2 * 3600
            except Exception:
                recently_expired_mature = False
        preserve_handled_flags = (
            old_status == SPIRIT_TREE_MATURE_STATUS
            and (old_harvested or old_attempted)
            and parsed_from_status
            and seconds > 0
            and (already_mature or recently_expired_mature)
        )
        fallback_until = old_until if old_until and is_future(old_until) else self.get_spirit_tree_irrigation_time(identity)
        if not (fallback_until and is_future(fallback_until)):
            fallback_until = ""
        candidate_until = add_seconds_str(now_str(), seconds) if seconds > 0 else fallback_until

        if seconds <= 0 and fallback_until:
            mature_until = fallback_until
        elif seconds <= 0:
            mature_until = add_seconds_str(now_str(), 600)
        elif already_mature and same_msg:
            mature_until = old_until
        elif already_mature and parsed_from_status:
            mature_until = candidate_until
        elif already_mature:
            mature_until = old_until
        else:
            mature_until = candidate_until
            if preserve_handled_flags:
                a_state["spirit_tree_harvested_in_mature_period"] = old_harvested
                a_state["spirit_tree_harvest_attempted_in_mature_period"] = old_attempted
            else:
                a_state["spirit_tree_harvested_in_mature_period"] = False
                a_state["spirit_tree_harvest_attempted_in_mature_period"] = False

        a_state["spirit_tree_status"] = SPIRIT_TREE_MATURE_STATUS
        a_state["spirit_tree_mature_until"] = mature_until
        if identity == "主魂":
            a_state["next_spirit_tree_irrigation_time"] = mature_until
        a_state["spirit_tree_last_mature_detected_time"] = now_str()
        a_state["spirit_tree_last_mature_msg_id"] = msg_id
        if SPIRIT_TREE_STATUS_COMMAND in self.clean_spirit_tree_text(text) or "采摘期" in self.clean_spirit_tree_text(text):
            a_state["spirit_tree_last_status_time"] = now_str()

        needs_harvest = not (
            a_state.get("spirit_tree_harvested_in_mature_period")
            or a_state.get("spirit_tree_harvest_attempted_in_mature_period")
            or no_irrigation_only
            or self.spirit_tree_harvest_locked(identity)
        )
        a_state["spirit_tree_harvest_pending"] = needs_harvest
        self.save_state()
        log.info(f"[{identity}] spirit tree status -> {SPIRIT_TREE_MATURE_STATUS} until {mature_until} ({source}).")
        return needs_harvest

    def record_spirit_tree_invasion_state(self, text, msg=None, source="", identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        a_state = self.spirit_tree_state_for_identity(identity)
        now = now_str()
        a_state["spirit_tree_invasion_status"] = "古剑门来袭"
        a_state["spirit_tree_guard_pending"] = True
        a_state["spirit_tree_last_invasion_time"] = now
        a_state["spirit_tree_last_invasion_msg_id"] = getattr(msg, "id", None) or 0
        clear_command_guard_block(
            self, SPIRIT_TREE_GUARD_COMMAND, log,
            reason="new spirit tree invasion",
        )
        self.save_state()
        log.info(f"[{identity}] 古剑门来袭 detected ({source}); scheduling {SPIRIT_TREE_GUARD_COMMAND}.")
        return True

    def record_spirit_tree_harvest_response(self, text, identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        a_state = self.spirit_tree_state_for_identity(identity)
        clean = self.clean_spirit_tree_text(text)
        if any(k in clean for k in ["灵果入腹", "摘下一枚", "采摘成功", "获得", "修为增长", "已经采摘", "已采摘", "本轮已采"]):
            a_state["spirit_tree_harvested_in_mature_period"] = True
            a_state["spirit_tree_harvest_pending"] = False
            a_state["spirit_tree_last_harvest_time"] = now_str()
            if not (a_state.get("next_spirit_tree_harvest_time", "") and is_future(a_state.get("next_spirit_tree_harvest_time", ""))):
                a_state["next_spirit_tree_harvest_time"] = add_seconds_str(now_str(), SPIRIT_TREE_HARVEST_LOCK_SECONDS)
            self.save_state()
            log.info(f"[{identity}] spirit tree harvest recorded.")
            return True
        if any(k in clean for k in [
            "尚未成熟", "还未成熟", "未成熟", "采摘期未开启", "尚未进入采摘期",
            "未曾为灵树灌溉", "无功不受禄",
        ]):
            existing_irrigation = self.get_spirit_tree_irrigation_time(identity)
            a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
            a_state["spirit_tree_mature_until"] = ""
            a_state["spirit_tree_harvested_in_mature_period"] = False
            a_state["spirit_tree_harvest_pending"] = False
            if not (existing_irrigation and is_future(existing_irrigation)):
                self.set_spirit_tree_irrigation_time(identity, add_seconds_str(now_str(), 600))
            self.save_state()
            log.info(f"[{identity}] spirit tree harvest rejected as not mature; resume irrigation checks.")
            return True
        if any(k in clean for k in ["核对天道榜单", "拿出宗门贡献令"]):
            log.info(f"[{identity}] spirit tree harvest settlement pending edited result.")
            return False
        if clean:
            notify_unrecognized_response(self, SPIRIT_TREE_HARVEST_COMMAND, clean, log, "灵树采摘")
        return False

    def record_spirit_tree_irrigation_state(self, text, source="", identity=SPIRIT_TREE_AVATAR, success_base_time=""):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        a_state = self.spirit_tree_state_for_identity(identity)
        clean = self.clean_spirit_tree_text(text)
        previous_status = a_state.get("spirit_tree_status", "")
        is_status_text = (
            ("灵眼之树" in clean or "灵树状态" in clean)
            and any(k in clean for k in ["进度", "阶段", "环境", "成熟度", "当前状态"])
        )
        is_success_text = "灵树灌溉" in clean and "成熟度" in clean
        is_cooldown_text = self.spirit_tree_text_is_irrigation_cooldown(clean)
        if not (is_status_text or is_success_text or is_cooldown_text):
            if clean:
                notify_unrecognized_response(self, SPIRIT_TREE_IRRIGATION_COMMAND, clean, log, "灵树灌溉")
            return False
        a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
        a_state["spirit_tree_mature_until"] = ""
        a_state["spirit_tree_harvested_in_mature_period"] = False
        a_state["spirit_tree_harvest_attempted_in_mature_period"] = False
        a_state["spirit_tree_harvest_pending"] = False
        if is_status_text:
            a_state["spirit_tree_last_status_time"] = now_str()

        if is_cooldown_text:
            cd = self.parse_wait_time(clean)
            self.set_spirit_tree_irrigation_time(identity, add_seconds_str(now_str(), cd if cd > 0 else 600))
        elif is_success_text:
            base_time = success_base_time or now_str()
            try:
                str_to_dt(base_time)
            except Exception:
                base_time = now_str()
            self.set_spirit_tree_irrigation_time(identity, add_seconds_str(base_time, 2 * 3600))
        elif previous_status == SPIRIT_TREE_MATURE_STATUS:
            self.set_spirit_tree_irrigation_time(identity, "")

        self.save_state()
        log.info(f"[{identity}] spirit tree status -> {SPIRIT_TREE_IRRIGATION_STATUS} ({source}).")
        return True

    def classify_spirit_tree_guard_response(self, text):
        clean = self.clean_spirit_tree_text(text)
        cd = self.parse_wait_time(clean)
        if not clean:
            return "", 0
        if any(k in clean for k in ["当前并无外敌入侵", "并无外敌入侵", "无需加固大阵", "无需加固", "无需守山"]):
            return "no_invasion", 0
        if "三派异动" in clean and "古剑门" in clean and not self.spirit_tree_text_indicates_invasion(clean):
            return "no_invasion", 0
        if any(k in clean for k in ["守山成功", "大阵修复", "为护山大阵注入"]):
            return "success", SPIRIT_TREE_GUARD_SUCCESS_RETRY_SECONDS
        if any(k in clean for k in ["经脉尚需调息", "后再来守山", "刚刚注入过灵力", "已协同", "冷却"]):
            return "cooldown", max(1, cd)
        return "", 0

    def record_spirit_tree_guard_response(self, text, identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        a_state = self.spirit_tree_state_for_identity(identity)
        clean = self.clean_spirit_tree_text(text)
        outcome, cd_seconds = self.classify_spirit_tree_guard_response(clean)
        now = now_str()
        if outcome == "no_invasion":
            a_state["spirit_tree_invasion_status"] = ""
            a_state["spirit_tree_guard_pending"] = False
            self.clear_spirit_tree_guard_times()
            force_command_guard_block(
                self,
                SPIRIT_TREE_GUARD_COMMAND,
                SPIRIT_TREE_GUARD_ERROR_BLOCK_SECONDS,
                log,
                reason="spirit_tree_no_invasion_response",
                alert=True,
                reason_text=(
                    f"返回信息表示当前无需守山，已暂停该命令 "
                    f"{SPIRIT_TREE_GUARD_ERROR_BLOCK_SECONDS // 60} 分钟。"
                ),
            )
            log.info(f"[{identity}] spirit tree guard stopped: no invasion.")
            return outcome
        if outcome in {"success", "cooldown"}:
            if outcome == "success":
                self.set_spirit_tree_last_guard_time(identity, now)
            self.set_spirit_tree_guard_time(identity, add_seconds_str(now, cd_seconds))
            a_state["spirit_tree_invasion_status"] = "古剑门来袭"
            a_state["spirit_tree_guard_pending"] = True
            self.save_state()
            log.info(
                f"[{identity}] spirit tree guard {outcome}; "
                f"next attempt after {self.get_spirit_tree_guard_time(identity)}."
            )
            return outcome
        if clean and not any(k in clean for k in ["协同守山", "护山", "古剑门", "冷却", "已协同", "加固", "守山"]):
            notify_unrecognized_response(self, SPIRIT_TREE_GUARD_COMMAND, clean, log, "协同守山")
        return ""

    def schedule_spirit_tree_harvest_once(self, reason="mature", identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        locked_until = self.spirit_tree_harvest_lock_until(identity)
        if locked_until:
            log.info(f"[{identity}] spirit tree harvest schedule skipped: locked until {locked_until} ({reason}).")
            return
        tasks = getattr(self, "_spirit_tree_harvest_tasks", None)
        if tasks is None:
            tasks = {}
            self._spirit_tree_harvest_tasks = tasks
        task = tasks.get(identity)
        if task and not task.done():
            return
        tasks[identity] = asyncio.create_task(self.execute_spirit_tree_harvest_once(reason, identity=identity))

    def spirit_tree_guard_identities(self, preferred_identity=None):
        identities = []

        def add(identity):
            identity = str(identity or "").strip()
            if identity and identity not in identities:
                identities.append(identity)

        add(preferred_identity)
        add("主魂")
        if SPIRIT_TREE_AVATAR in getattr(self, "avatars", []):
            features = (getattr(self, "avatar_features", {}) or {}).get(SPIRIT_TREE_AVATAR, {})
            if features.get("spirit_tree_irrigation"):
                add(SPIRIT_TREE_AVATAR)
        return identities

    def schedule_spirit_tree_guard_opportunities(self, reason="invasion", preferred_identity=None):
        for identity in self.spirit_tree_guard_identities(preferred_identity):
            next_guard = self.get_spirit_tree_guard_time(identity)
            delay = seconds_until(next_guard) if next_guard and is_future(next_guard) else 0
            self.schedule_spirit_tree_guard_once(reason, identity=identity, delay_seconds=max(0, int(delay)))

    def schedule_spirit_tree_guard_once(self, reason="invasion", identity=SPIRIT_TREE_AVATAR, delay_seconds=0):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        tasks = getattr(self, "_spirit_tree_guard_tasks", None)
        if tasks is None:
            tasks = {}
            self._spirit_tree_guard_tasks = tasks
        task = tasks.get(identity)
        current = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task and not task.done() and task is not current:
            return
        tasks[identity] = asyncio.create_task(
            self.execute_spirit_tree_guard_once(reason, identity=identity, delay_seconds=delay_seconds)
        )

    def maybe_record_spirit_tree_passive_message(self, msg, text, source="passive", identity=None, irrigation_success_base_time=""):
        global_tree_state = self.spirit_tree_text_is_global_state(text)
        target_identity = str(identity or "").strip()
        if not target_identity:
            target_identity = self.spirit_tree_identity_from_message(msg, text) or "主魂"
        if is_reply_to_untracked_message(self, msg) and not global_tree_state:
            return False
        indicates_mature = self.spirit_tree_text_indicates_mature(text)
        indicates_irrigation = self.spirit_tree_text_indicates_irrigation_state(text)
        indicates_invasion = self.spirit_tree_text_indicates_invasion(text)
        if (
            indicates_irrigation
            and not global_tree_state
            and not self.spirit_tree_irrigation_message_is_trusted(target_identity, msg, text, source=source)
        ):
            log.info(f"[{target_identity}] spirit tree irrigation sync skipped ({source}): untrusted ownership.")
            return False
        if (
            (indicates_mature or indicates_irrigation or indicates_invasion)
            and not global_tree_state
            and not self.spirit_tree_message_targets_identity(target_identity, msg, text, source=source)
        ):
            log.info(f"[{target_identity}] spirit tree sync skipped ({source}): message is not targeted to this identity.")
            return False

        matched = False
        if indicates_mature:
            needs_harvest = self.record_spirit_tree_mature_state(text, msg=msg, source=source, identity=target_identity)
            if needs_harvest:
                self.schedule_spirit_tree_harvest_once(source, identity=target_identity)
            matched = True
        elif indicates_irrigation:
            self.record_spirit_tree_irrigation_state(
                text,
                source=source,
                identity=target_identity,
                success_base_time=irrigation_success_base_time,
            )
            matched = True
        if indicates_invasion:
            needs_guard = self.record_spirit_tree_invasion_state(text, msg=msg, source=source, identity=target_identity)
            if needs_guard:
                self.schedule_spirit_tree_guard_opportunities(source, preferred_identity=target_identity)
            matched = True
        if not matched:
            self.normalize_spirit_tree_state(target_identity)
        return matched

    async def refresh_spirit_tree_status_after_no_irrigation(self, identity, source="irrigation response"):
        """When irrigation says no action is needed, query status to get the real mature timer."""
        identity = str(identity or "主魂").strip() or "主魂"
        log.info(
            f"[{identity}] {SPIRIT_TREE_IRRIGATION_COMMAND} returned no-irrigation; "
            f"querying {SPIRIT_TREE_STATUS_COMMAND}."
        )
        status_resp = await self.send_and_wait_feedback_identity(
            identity,
            SPIRIT_TREE_STATUS_COMMAND,
            timeout=60,
            max_retries=1,
        )
        status_text = self.response_text(status_resp)
        if status_text and self.maybe_record_spirit_tree_passive_message(
            None,
            status_text,
            source=f"{source} status follow-up",
            identity=identity,
        ):
            return True
        log.warning(f"[{identity}] {SPIRIT_TREE_STATUS_COMMAND} follow-up did not return recognizable status.")
        return False

    def spirit_tree_identity_from_message(self, msg, text):
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
        if marker:
            return marker.group(1).strip()

        reply_identity = tracked_command_identity_for_reply(self, msg)
        if reply_identity:
            return reply_identity

        lower_text = str(text or "").lower()
        for identity, usernames in (getattr(self, "identity_usernames", None) or {}).items():
            for username in usernames or []:
                name = str(username or "").lower().lstrip("@").strip()
                if name and f"@{name}" in lower_text:
                    return identity
        for username, identity in (getattr(self, "avatar_usernames", None) or {}).items():
            name = str(username or "").lower().lstrip("@").strip()
            if name and f"@{name}" in lower_text:
                return identity
        return ""

    def spirit_tree_irrigation_message_is_trusted(self, identity, msg, text, source=""):
        """Irrigation success/cooldown is per identity; do not consume stray group replies."""
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        if msg is None:
            return True
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
        if marker:
            return marker.group(1).strip() == identity
        reply_identity = tracked_command_identity_for_reply(self, msg)
        if reply_identity:
            return reply_identity == identity

        lower_text = str(text or "").lower()
        if identity == "主魂":
            for username in (getattr(self, "identity_usernames", None) or {}).get("主魂", []):
                name = str(username or "").lower().lstrip("@").strip()
                if name and (f"@{name}" in lower_text or f"【{name}】" in lower_text):
                    return True
        for username, avatar_identity in (getattr(self, "avatar_usernames", None) or {}).items():
            if avatar_identity != identity:
                continue
            name = str(username or "").lower().lstrip("@").strip()
            if name and (f"@{name}" in lower_text or f"【{name}】" in lower_text):
                return True
        return False

    def spirit_tree_message_targets_identity(self, identity, msg, text, source=""):
        """Only accept spirit-tree bot text that can be attributed to the expected identity."""
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
        if marker:
            return marker.group(1).strip() == identity
        if msg is None:
            return True

        reply_identity = tracked_command_identity_for_reply(self, msg)
        if reply_identity:
            return reply_identity == identity

        if mentions_other_user_for_identity(self, msg, text, identity):
            return False

        if identity == "主魂":
            return True

        lower_text = str(text or "").lower()
        for username, avatar_identity in (self.avatar_usernames or {}).items():
            if avatar_identity != identity:
                continue
            name = str(username or "").lower().lstrip("@").strip()
            if not name:
                continue
            if f"@{name}" in lower_text or f"【{name}】" in lower_text:
                return True
            if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lower_text):
                return True

        return False

    def spirit_tree_message_targets_avatar(self, msg, text, source=""):
        return self.spirit_tree_message_targets_identity(SPIRIT_TREE_AVATAR, msg, text, source=source)

    async def execute_spirit_tree_harvest_once(self, reason="mature", identity=SPIRIT_TREE_AVATAR):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        await self.startup_done.wait()
        await self.pause_event.wait()
        async with AtomicTaskContext(self, f"SpiritTreeHarvest-{identity}"):
            a_state = self.normalize_spirit_tree_state(identity)
            mature_until = a_state.get("spirit_tree_mature_until", "")
            if a_state.get("spirit_tree_status") != SPIRIT_TREE_MATURE_STATUS or not (mature_until and is_future(mature_until)):
                return
            locked_until = self.spirit_tree_harvest_lock_until(identity)
            if locked_until:
                log.info(f"[{identity}] spirit tree harvest skipped: locked until {locked_until} ({reason}).")
                a_state["spirit_tree_harvest_pending"] = False
                self.save_state()
                return
            if a_state.get("spirit_tree_harvested_in_mature_period") or a_state.get("spirit_tree_harvest_attempted_in_mature_period"):
                return
            self.record_spirit_tree_harvest_attempt(identity, reason)
            log.info(f"[{identity}] spirit tree mature detected ({reason}); sending {SPIRIT_TREE_HARVEST_COMMAND} once.")
            resp = await self.send_and_wait_feedback_identity(identity, SPIRIT_TREE_HARVEST_COMMAND, timeout=90, max_retries=0)
            resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            if self.record_spirit_tree_harvest_response(resp_text, identity=identity):
                return
            resp_id = getattr(resp, "id", None)
            if not resp_id:
                return
            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    updated_msg = await self.client.get_messages(self.target_chat_id, ids=resp_id)
                except Exception as e:
                    log.info(f"[{identity}] spirit tree harvest edit poll failed: {e}")
                    return
                updated_text = (updated_msg.text or "") if updated_msg else ""
                if updated_text and updated_text != resp_text:
                    resp_text = updated_text
                    if self.record_spirit_tree_harvest_response(resp_text, identity=identity):
                        return

    async def execute_spirit_tree_guard_once(self, reason="invasion", identity=SPIRIT_TREE_AVATAR, delay_seconds=0):
        identity = str(identity or SPIRIT_TREE_AVATAR).strip() or SPIRIT_TREE_AVATAR
        await self.startup_done.wait()
        await self.pause_event.wait()
        if delay_seconds and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
            await self.pause_event.wait()
        async with AtomicTaskContext(self, f"SpiritTreeGuard-{identity}"):
            a_state = self.spirit_tree_state_for_identity(identity)
            if self.identity_pause_seconds(identity) > 0:
                log.info(f"[{identity}] spirit tree guard skipped: identity paused.")
                return
            next_guard = self.get_spirit_tree_guard_time(identity)
            if next_guard and is_future(next_guard):
                delay = max(1, int(seconds_until(next_guard)))
                self.schedule_spirit_tree_guard_once(reason, identity=identity, delay_seconds=delay)
                return
            if not a_state.get("spirit_tree_guard_pending") and a_state.get("spirit_tree_invasion_status") != "古剑门来袭":
                return
            if not command_send_precheck(self, SPIRIT_TREE_GUARD_COMMAND, log, identity=identity):
                log.info(f"[{identity}] spirit tree guard skipped: command is not sendable now.")
                return
            log.info(f"[{identity}] 古剑门来袭 detected ({reason}); sending {SPIRIT_TREE_GUARD_COMMAND} once.")
            resp = await self.send_and_wait_feedback_identity(identity, SPIRIT_TREE_GUARD_COMMAND, timeout=90, max_retries=1)
            resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            outcome = self.record_spirit_tree_guard_response(resp_text, identity=identity)
            if outcome in {"success", "cooldown"}:
                next_retry = self.get_spirit_tree_guard_time(identity)
                delay = max(1, int(seconds_until(next_retry))) if next_retry and is_future(next_retry) else 1
                self.schedule_spirit_tree_guard_once(f"{reason} retry", identity=identity, delay_seconds=delay)

    def text_targets_self(self, msg, text):
        """判断消息是否针对本账号"""
        if mentions_self(self, msg, text): return True
        lower_text = (text or "").lower()
        candidates = []
        if self.my_info: candidates.extend([getattr(self.my_info, "username", "") or "", getattr(self.my_info, "first_name", "") or ""])
        candidates.extend(self.notify_users)
        for value in candidates:
            name = str(value or "").lower().lstrip("@").strip()
            if not name: continue
            if f"@{name}" in lower_text or f"【{name}】" in lower_text: return True
            if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lower_text): return True
        return False

    def update_identity_passively(self, msg):
        """
        从 bot 回复中被动探测当前身份。
        当检测到切换成功的回复时，自动修正 self.current_identity。
        """
        text = msg.text or ""
        if not text: return
        if is_reply_to_untracked_message(self, msg): return
        if not self.text_targets_self(msg, text): return
        # 检测切换回主魂
        if "神念重归主魂肉身" in text or ("主魂" in text and ("成功" in text or "已切换" in text or "当前操控" in text)):
            if self.current_identity != "主魂":
                log.info(f"Identity passively updated: {self.current_identity} -> 主魂")
                self.current_identity = "主魂"
            self._manual_identity_label = "主魂"
            return
        # 检测切换到化身
        if "切换" in text or "当前操控" in text:
            for avatar in self.avatars:
                if avatar in text and ("成功" in text or "已切换" in text or "当前操控" in text):
                    if self.current_identity != avatar:
                        log.info(f"Identity passively updated: {self.current_identity} -> {avatar}")
                        self.current_identity = avatar
                        self._main_confirmed = False  # 被动化身切换，主魂确认失效
                    self._manual_identity_label = avatar
                    return

    def maybe_record_avatar_passive_states(self, msg):
        """解析并记录手动发送指令引发的状态变更"""
        text = msg.text or ""
        if not text: return
        if is_reply_to_untracked_message(self, msg): return
        if self.record_passive_concubine_voyage_response(text):
            log.info("Passive concubine voyage state synced from loose bot message.")
            return
        recent_identity = recent_profile_identity_for_text(self, text, msg_id=getattr(msg, "id", None))

        avatar = recent_identity or None
        attribution_reliable = bool(recent_identity)
        # Explicit bot-side avatar markers are reliable even when the message does not @ the account.
        if not avatar:
            if "[Avatar: 无咎子]" in text: avatar = "无咎子"; attribution_reliable = True
            elif "[Avatar: 缘生子]" in text: avatar = "缘生子"; attribution_reliable = True
            elif "[Avatar: 素缘子]" in text: avatar = "素缘子"; attribution_reliable = True
            elif "神念重归主魂肉身" in text or "当前操控：主魂" in text: avatar = "主魂"; attribution_reliable = True
        if not self.text_targets_self(msg, text) and not recent_identity and not attribution_reliable:
            return
        # 优先根据 sender_id 判断发送者（化身有独立 chat_id）
        sender_id = str(getattr(msg, "sender_id", ""))
        if not avatar and sender_id == "-1004240160265":
            avatar = "无咎子"; attribution_reliable = True
        elif not avatar and sender_id == "-1003809391782":
            avatar = "缘生子"; attribution_reliable = True
        elif not avatar and sender_id == "-1003999815554":
            avatar = "素缘子"; attribution_reliable = True

        # 机器人被动结算经常只写 @用户名，不一定带 [Avatar: ...] 或 reply_to。
        if not avatar:
            lower_text = text.lower()
            for username, name in self.avatar_usernames.items():
                if f"@{str(username).lower()}" in lower_text:
                    avatar = name
                    attribution_reliable = True
                    break
            if not avatar:
                for username in self.identity_usernames.get("主魂", []):
                    if f"@{str(username).lower()}" in lower_text:
                        avatar = "主魂"
                        attribution_reliable = True
                        break

        # 退化到 reply_to 查找
        if not avatar:
            reply_to = getattr(msg, 'reply_to', None)
            if reply_to:
                reply_to_id = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'channel_post', None)
                if reply_to_id and reply_to_id in self.command_avatar_map:
                    avatar = self.command_avatar_map.get(reply_to_id)
                    attribution_reliable = True
        # 最后 fallback: current_identity（不可靠）
        if not avatar:
            avatar = self.current_identity
            attribution_reliable = False

        main_text_targets_self = False
        lower_text = text.lower()
        for username in self.identity_usernames.get("主魂", []):
            if f"@{str(username).lower().lstrip('@')}" in lower_text:
                main_text_targets_self = True
                break
        meditation_text_owned = attribution_reliable or (avatar == "主魂" and main_text_targets_self)

        # 统一解析境界和修为（主魂+化身都更新）
        # ⚠️ 必须有 attribution_reliable 守卫！
        # current_identity fallback 不可靠——主魂/他人发的指令可能污染化身数据
        if attribution_reliable:
            record_cultivation_profile_from_text(
                self, text, identity=avatar, logger=log, source="passive profile"
            )

        now = now_str()

        if avatar == "主魂" or attribution_reliable:
            if self.record_yuanying_out_active_response(text, source=f"passive {avatar}", identity=avatar):
                self.save_state()
            elif self.record_yuanying_out_settlement_response(text, source=f"passive {avatar}", identity=avatar):
                self.save_state()

        # ---- 闭关相关（主魂+化身） ----
        # 强行出关 / 明确闭关结算 → 清除深度闭关状态
        _is_force_exit = any(k in text for k in ["强行出关", "强行中断", "强行出关惩罚"])
        _is_real_exit = _is_force_exit or any(k in text for k in ["出关成功", "已出关", "闭关结束"])
        _is_deep_settlement = is_deep_meditation_settlement_response(text)
        if _is_real_exit or _is_deep_settlement:
            if not meditation_text_owned:
                log.info(f"[{avatar}] passive: ignored unowned meditation exit text.")
            elif avatar == "主魂":
                self.state["in_deep_meditation"] = False
                self.state["deep_meditation_end_time"] = ""
                self.state["deep_meditation_guard_until"] = ""
                self.state["is_closing"] = False
                self.meditation_state_event.set()
                log.info(f"[{avatar}] passive: closing state cleared (出关).")
            else:
                self.mark_avatar_meditation_restart_pending(
                    avatar,
                    "passive force exit" if _is_force_exit else "passive settlement",
                )
                log.info(f"[{avatar}] passive: closing state cleared (出关).")

        # 深度闭关中 / 预计还需 → 更新深度闭关结束时间
        elif any(k in text for k in ["深度闭关", "预计还需", "闭关修炼"]):
            is_ongoing = any(k in text for k in ["预计还需", "还需"])
            if not meditation_text_owned:
                log.info(f"[{avatar}] passive: ignored unowned deep meditation text.")
            elif is_not_deep_meditation_response(text) or is_deep_meditation_settlement_response(text):
                if avatar == "主魂":
                    self.state["in_deep_meditation"] = False
                    self.state["deep_meditation_end_time"] = ""
                    self.state["deep_meditation_guard_until"] = ""
                    self.meditation_state_event.set()
                else:
                    self.mark_avatar_meditation_restart_pending(avatar, "passive settlement")
                log.info(f"[{avatar}] passive: deep meditation ended.")
            else:
                cd = self.parse_wait_time(text)
                if cd > 0:
                    end_time = add_seconds_str(now, cd)
                    if avatar == "主魂":
                        self.state.update(self.meditation_active_state_values(end_time, clear_restart=False))
                        self.meditation_state_event.set()
                    else:
                        self.update_avatar_states(avatar, self.meditation_active_state_values(end_time))
                    log.info(f"[{avatar}] passive: deep meditation active, {cd}s remaining.")

        # 闭关冷却中 → 更新 next_meditation_time
        if "闭关冷却" in text or "闭关剩余" in text:
            if not meditation_text_owned:
                log.info(f"[{avatar}] passive: ignored unowned meditation cooldown text.")
            else:
                cd = self.parse_wait_time(text, line_identifier="闭关")
                if cd > 0:
                    if avatar == "主魂":
                        self.state["next_meditation_time"] = add_seconds_str(now, cd)
                    else:
                        self.set_avatar_state(avatar, "next_meditation_time", add_seconds_str(now, cd))
                    log.info(f"[{avatar}] passive: meditation cooldown {cd}s.")

        # ---- 闯塔（主魂+化身） ----
        if "通关" in text and ("层" in text or "塔" in text):
            today = datetime.now().strftime("%Y-%m-%d")
            if avatar == "主魂":
                self.state["last_tower_date"] = today
            else:
                self.set_avatar_state(avatar, "last_tower_date", today)
            log.info(f"[{avatar}] passive: tower cleared today.")
        elif "闯塔冷却" in text or "今日已闯" in text:
            today = datetime.now().strftime("%Y-%m-%d")
            if avatar == "主魂":
                self.state["last_tower_date"] = today
            else:
                self.set_avatar_state(avatar, "last_tower_date", today)

        # ---- 入梦寻图（化身为主） ----
        if "当前进度：" in text and "残图" in text and "拼图" in text:
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now, 8 * 3600))
            log.info(f"[{avatar}] passive: dream map success, cooldown 8h.")
        elif "入梦寻图冷却:" in text or "入梦寻图冷却：" in text:
            match = re.search(r"入梦寻图冷却[：:]\s*([^\n]+)", text)
            if match:
                val = match.group(1).strip()
                if "可施展" in val or "无" in val or "可用" in val:
                    self.set_avatar_state(avatar, "next_dream_map_time", now)
                else:
                    cd = self.parse_wait_time(val)
                    if cd > 0:
                        self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now, cd + 60))

        # ---- 共历心劫（化身为主） ----
        if "坠魔心劫" in text and "第一轮" in text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, 10 * 3600))
            log.info(f"[{avatar}] passive: heart trial started, cooldown 10h.")
        elif "心劫余波未散" in text or "心劫冷却" in text:
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, cd))

        # ---- 侍妾远航（仅星宫道心侍妾身份） ----
        if self.record_concubine_voyage_response(text, identity=avatar):
            log.info(f"[{avatar}] passive: concubine voyage state synced.")

        # ---- 九天罡风 ----
        if "罡风" in text and ("虚弱" in text or "元婴受创" in text):
            log.info(f"[{avatar}] passive: detected 罡风 weakness warning.")

        # ---- 每日任务 ----
        if "今日任务已完成" in text or "所有任务已完成" in text:
            today = datetime.now().strftime("%Y-%m-%d")
            if avatar == "主魂":
                self.state["last_daily_date"] = today
            else:
                self.set_avatar_state(avatar, "last_daily_date", today)
            log.info(f"[{avatar}] passive: daily tasks completed today.")

        # ---- 星宫化身：观星台 / 牵引 / 安抚 / 收集。脚本和手动回复都走这里对账。----
        if avatar in STAR_ATTRACTION_AVATARS:
            self.record_avatar_star_response_from_text(avatar, text, source="passive star sync")

        # 保存状态
        self.save_state()

    def get_identity_from_msg(self, msg):
        if not msg or not hasattr(msg, "sender_id"):
            return None
        return self.avatar_identities.get(str(msg.sender_id))

    def _state_impending_command_wait(self, state, identity=""):
        """Return seconds until the next command-worthy timestamp for an identity, or -1."""
        if not isinstance(state, dict):
            return -1
        ignored_keys = {
            "next_switch_allowed_time",
            "sect_war_active_until",
            "formation_active_until",
            "yuanying_out_end_time",
            "pending_star_gazing_date",
            "last_star_shift_date",
        }
        watch_keys = {
            "deep_meditation_end_time",
            "nine_heaven_wind_cd_time",
            "heart_platform_time",
        }
        min_wait = None
        features = self.avatar_features.get(identity, {}) if identity in self.avatars else {}
        for key, value in state.items():
            if key in ("next_formation_time", "next_formation_retry_time") and identity in self.avatars and not features.get("formation"):
                continue
            if key == "next_yuanying_out_time" and identity in self.avatars and not features.get("yuanying_out"):
                continue
            if key == "next_rift_search_time" and identity in self.avatars and not features.get("rift_search"):
                continue
            if key in ("next_heart_time", "heart_platform_time"):
                today = datetime.now().strftime("%Y-%m-%d")
                if state.get("heart_platform_date") == today:
                    continue
                if hasattr(self, "is_heart_platform_fallback_due") and not self.is_heart_platform_fallback_due(today):
                    continue
            if key == "next_force_exit_time":
                active_until = state.get("formation_active_until", "")
                if not (active_until and is_future(active_until)):
                    continue
            if key == "next_concubine_voyage_time" and not self.concubine_voyage_enabled(identity):
                continue
            if (
                key == "next_concubine_voyage_time"
                and not self.concubine_voyage_auto_start_enabled(identity)
                and not state.get("concubine_voyage_active")
            ):
                continue
            if key in ignored_keys or not isinstance(value, str) or not value:
                continue
            if key == "deep_meditation_end_time" and not state.get("in_deep_meditation"):
                continue
            if key not in watch_keys and not self.state_time_command_for_key(key):
                continue
            if self.state_time_command_paused(key, identity):
                continue
            try:
                wait = seconds_until(value) if is_future(value) else 0
            except Exception:
                continue
            min_wait = wait if min_wait is None else min(min_wait, wait)

        today = datetime.now().strftime("%Y-%m-%d")
        if identity == "主魂":
            done = set(state.get("done", [])) if isinstance(state.get("done"), list) else set()
            daily_due = (
                (".宗门点卯" not in done and not self.dashboard_command_paused(".宗门点卯", identity))
                or (".闯塔" not in done and not self.dashboard_command_paused(".闯塔", identity))
            )
            if seconds_until_daily_task_start(datetime.now()) <= 0 and daily_due:
                min_wait = 0 if min_wait is None else min(min_wait, 0)
        elif identity in self.avatars:
            features = self.avatar_features.get(identity, {})
            if (
                features.get("daily_checkin")
                and state.get("last_dianmao_date") != today
                and not self.dashboard_command_paused(".宗门点卯", identity)
                and seconds_until_daily_task_start(datetime.now()) <= 0
            ):
                min_wait = 0 if min_wait is None else min(min_wait, 0)
            if (
                features.get("tower")
                and state.get("last_tower_date") != today
                and not self.dashboard_command_paused(".闯塔", identity)
                and datetime.now().hour >= 23
            ):
                min_wait = 0 if min_wait is None else min(min_wait, 0)
            if (
                not state.get("in_deep_meditation")
                and not state.get("deep_meditation_end_time")
                and not state.get("next_meditation_retry_time")
            ):
                min_wait = 0 if min_wait is None else min(min_wait, 0)

        return min_wait if min_wait is not None else -1

    def get_identity_impending_command_wait(self, identity):
        if self.identity_pause_seconds(identity) > 0:
            return 999999
        if identity == "主魂":
            wait = self._state_impending_command_wait(self.state, identity="主魂")
            return self.merge_impending_wait(wait, self.custom_command_impending_wait("主魂"))
        if identity in self.avatars:
            wait = self._state_impending_command_wait(self.get_avatar_state(identity), identity=identity)
            return self.merge_impending_wait(wait, self.custom_command_impending_wait(identity))
        return -1

    def get_avatar_impending_command_wait(self, avatar):
        return self.get_identity_impending_command_wait(avatar)

    @property
    def current_identity(self):
        return self._current_identity

    @current_identity.setter
    def current_identity(self, value):
        self._current_identity = value
        self.state["current_identity"] = value
        self.save_state()

    # ============================================================
    # 身外化身：身份切换
    # ============================================================

    async def send_and_wait_feedback_identity(self, identity, message, timeout=45, max_retries=2, **kwargs):
        """带身份感知的指令发送：先切换到目标化身，再发送指令"""
        force_identity_check = bool(kwargs.pop("force_identity_check", False))
        force_meditation_check = bool(kwargs.pop("force_meditation_check", False))
        high_priority_identity_command = self.time_critical_identity_command(message)
        allow_unconfirmed_switch = str(message).startswith(".改换星移")
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        if str(message or "").strip() == ".查看闭关" and identity in self.avatars and not force_meditation_check:
            guarded_resp = self.early_meditation_check_response_for_state(
                identity, self.get_avatar_state(identity), log
            )
            if guarded_resp:
                return guarded_resp

        # 暂停阻断守卫
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused(identity, message):
            return None

        yield_attempts = 0
        defer_started_at = None
        urgent_yield_attempts = 0
        urgent_defer_started_at = None
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            switched_this_iteration = False
            async with self.avatar_send_lock:
                if self.current_identity != identity:
                    fishing_wait = await self.fishing_switch_wait_or_raise_due(
                        self.current_identity, target_identity=identity, command=message
                    )
                    if fishing_wait > 0:
                        log.info(
                            f"Avatar switch deferred: {self.current_identity} is fishing; "
                            f"wait {fishing_wait:.1f}s for .提竿 before switching to {identity}."
                        )
                        should_yield = True
                        wait_sec_to_sleep = fishing_wait

                    wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                    if not should_yield and 0 <= wait_sec <= 60 and not high_priority_identity_command:
                        if defer_started_at is None:
                            defer_started_at = time.monotonic()
                        deferred_for = time.monotonic() - defer_started_at
                        if deferred_for < 60:
                            if yield_attempts == 0 or yield_attempts % 12 == 0:
                                log.info(
                                    f"Avatar switch deferred: {self.current_identity} has commands due "
                                    f"in {wait_sec:.1f}s. [{identity}] waits."
                                )
                            yield_attempts += 1
                            should_yield = True
                            wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))
                        else:
                            log.info(
                                f"Avatar switch to {identity} proceeds after {deferred_for:.1f}s defer; "
                                f"{self.current_identity} still reports due commands."
                            )

                    if not should_yield:
                        switch_cmd = f".切换 {identity}" if identity != "主魂" else ".切换 主魂"
                        log.info(f"Avatar switch: {self.current_identity} -> {identity}")
                        switch_resp = await self._send_and_wait_feedback_raw(
                            switch_cmd,
                            timeout=5 if high_priority_identity_command else 30,
                            max_retries=0 if high_priority_identity_command else 2,
                            suppress_no_response_alert=high_priority_identity_command,
                        )
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        passively_confirmed = self.current_identity == identity
                        if passively_confirmed:
                            log.info(f"✅ Avatar switch passively confirmed: now {identity}")
                        if (not passively_confirmed) and (not resp_str or not any(k in resp_str for k in ["成功", "已切换", "当前操控", identity])):
                            if allow_unconfirmed_switch:
                                log.warning(
                                    f"Avatar switch to {identity} has no confirmed feedback; "
                                    f"continuing for high-priority {message}."
                                )
                            else:
                                log.error(f"Avatar switch to {identity} FAILED!")
                                return None
                        self.current_identity = identity
                        self._main_confirmed = (identity == "主魂")
                        switched_this_iteration = True
                        log.info(f"Avatar switch confirmed: now {identity}; sending pending command immediately.")
                if not should_yield and not switched_this_iteration:
                    critical_wait = self.time_critical_defer_wait(identity, message, timeout=timeout)
                    if critical_wait >= 0:
                        if urgent_defer_started_at is None:
                            urgent_defer_started_at = time.monotonic()
                        urgent_deferred_for = time.monotonic() - urgent_defer_started_at
                        if urgent_deferred_for < 120:
                            if urgent_yield_attempts == 0 or urgent_yield_attempts % 12 == 0:
                                log.info(
                                    f"[{identity}] command [{message}] deferred: time-critical command due "
                                    f"in {critical_wait:.1f}s."
                                )
                            urgent_yield_attempts += 1
                            should_yield = True
                            wait_sec_to_sleep = max(
                                1,
                                min(10, critical_wait - 5 if critical_wait > 10 else critical_wait + 1),
                            )
                        else:
                            log.info(
                                f"[{identity}] command [{message}] proceeds after "
                                f"{urgent_deferred_for:.1f}s time-critical defer."
                            )

                if not should_yield:
                    resp = await self._send_and_wait_feedback_raw(message, timeout=timeout, max_retries=max_retries, **kwargs)
                    # 解析化身境界：仅在 current_identity 与目标一致时更新，防止污染
                    if resp and identity in self.avatars and self.current_identity == identity:
                        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
                        if resp_text:
                            record_cultivation_profile_from_text(
                                self, resp_text, identity=identity, logger=log, source=f"identity {message}"
                            )
                    return resp

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

    async def switch_back_to_main(self, force=False):
        """
        兼容旧调用的主魂切换钩子。

        默认不主动切回主魂；真正需要发送主魂指令时，send_and_wait_feedback()
        会在指令预检通过后再对齐身份，避免流程收尾阶段产生无后续指令的空切换。
        """
        if self._main_confirmed or self.current_identity == "主魂":
            return
        if not force:
            return
        if self.identity_pause_seconds("主魂") > 0:
            log.info("switch_back_to_main skipped: main soul is paused.")
            return
        if self.avatar_send_lock.locked():
            return
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task():
            await asyncio.sleep(0.5)

        if self._main_confirmed:
            return  # 已确认在主魂，跳过
        async with self._switch_lock:
            # 加锁后再检查一次——可能其他任务已经完成了切换
            if self._main_confirmed:
                return
            if self.current_identity in self.avatars:
                wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                if 0 <= wait_sec <= 60:
                    log.info(
                        f"switch_back_to_main deferred: {self.current_identity} "
                        f"has commands due in {wait_sec:.1f}s."
                    )
                    return
            try:
                async with self.avatar_send_lock:
                    resp = await self._send_and_wait_feedback_raw(".切换 主魂", timeout=10, max_retries=0)
                    resp_str = str(resp) if resp else ""
                    if any(k in resp_str for k in ["成功", "已切换", "主魂", "当前操控"]):
                        log.info(f"switch_back_to_main: confirmed ({self.current_identity} -> 主魂).")
                        self._main_confirmed = True
                    else:
                        log.warning(f"switch_back_to_main: unconfirmed: {resp_str[:80]}, forcing reset.")
            except Exception as e:
                log.error(f"switch_back_to_main failed: {e}, forcing identity reset.")
            self.current_identity = "主魂"

    # ============================================================
    # 身外化身：深度闭关
    # ============================================================

    async def record_avatar_deep_meditation_start(self, avatar, response_text):
        if not self.is_deep_meditation_start_success(response_text):
            self.set_avatar_state(avatar, "in_deep_meditation", False)
            self.set_avatar_state(avatar, "meditation_restart_pending", True)
            return False
        cd = self.parse_wait_time(response_text)
        if cd <= 0:
            verify_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
            verify_text = getattr(verify_resp, "text", "") if hasattr(verify_resp, "text") else verify_resp if isinstance(verify_resp, str) else str(verify_resp) if verify_resp else ""
            verify_cd = self.parse_wait_time(verify_text)
            if verify_cd > 0 and is_deep_meditation_ongoing_response(verify_text):
                cd = verify_cd
        self.update_avatar_states(
            avatar,
            self.meditation_active_state_values(
                add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600)
            ),
        )
        return True

    async def restart_avatar_deep_meditation_direct(self, avatar, reason=""):
        """强行出关后只需要直接补 .深度闭关，不先走 .闭关修炼。"""
        log.info(f"[{avatar}] restarting deep meditation directly ({reason}).")
        async with self.common_atomic_task(f"Meditation-{avatar}"):
            resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关", timeout=60, max_retries=1)
            resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            started = await self.record_avatar_deep_meditation_start(avatar, resp_text)
        if started:
            log.info(f"[{avatar}] direct deep meditation restart complete ({reason}).")
            return True

        if not resp_text:
            self.update_avatar_states(avatar, {
                "meditation_restart_pending": True,
                "meditation_restart_mode": "deep_only",
                "next_meditation_retry_time": add_seconds_str(now_str(), 600),
            })
            log.info(f"[{avatar}] direct deep meditation restart got no response; retry later.")
            return True

        if any(k in resp_text for k in ["先闭关", "闭关修炼", "普通闭关"]):
            self.set_avatar_state(avatar, "meditation_restart_mode", "")
            return False

        cd = self.parse_wait_time(resp_text)
        if cd > 0:
            self.update_avatar_states(avatar, {
                "meditation_restart_pending": True,
                "meditation_restart_mode": "deep_only",
                "next_meditation_retry_time": add_seconds_str(now_str(), cd),
            })
            log.info(f"[{avatar}] direct deep meditation restart deferred {cd}s ({reason}).")
            return True
        return False

    # ============================================================
    # 身外化身：修为不足
    # ============================================================

    async def handle_修为不足(self, avatar, retry_func, retry_args=None, retry_kwargs=None, cooldown_key="next_heart_trial_time", cooldown_hours=2):
        await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=30, return_response_msg=False)
        self.set_avatar_state(avatar, "in_deep_meditation", False)
        await asyncio.sleep(5)
        resp = await retry_func(*(retry_args or []), **(retry_kwargs or {}))
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else str(resp) if resp else ""
        if "修为不足" in resp_text:
            self.set_avatar_state(avatar, cooldown_key, add_seconds_str(now_str(), cooldown_hours * 3600))
            await asyncio.sleep(3)
            features = self.avatar_features.get(avatar, {})
            prefix = features.get("meditation_prefix", "")
            deep_cmd = f"{prefix} .深度闭关".strip() if prefix else ".深度闭关"
            deep_resp = await self.send_and_wait_feedback_identity(avatar, deep_cmd, timeout=60, return_response_msg=False)
            await self.record_avatar_deep_meditation_start(avatar, self.response_text(deep_resp))
            return False, resp_text
        return True, resp_text

    # ============================================================
    # 身外化身：共历心劫
    # ============================================================

    async def execute_avatar_heart_trial(self, avatar, status_msg):
        async with AtomicTaskContext(self, f"HeartTrial-{avatar}"):
            status_text = getattr(status_msg, "text", "") if hasattr(status_msg, "text") else ""
            if status_text and not self.concubine_status_matches_identity(status_text, avatar):
                log.warning(f"Avatar [{avatar}] heart trial: mismatched .我的侍妾 status; retry soon.")
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 30))
                return False
            voyage_block_until = self.parse_concubine_voyage_status_line(status_text, avatar)
            if voyage_block_until:
                self.set_avatar_state(avatar, "next_heart_trial_time", voyage_block_until)
                log.info(f"Avatar [{avatar}] heart trial blocked by active voyage until {voyage_block_until}.")
                return False
            trial_resp = await self.send_and_wait_feedback_identity(avatar, ".共历心劫", reply_to=status_msg.id, timeout=90, return_response_msg=True, delete_after=False)
            trial_text = (getattr(trial_resp, "text", "") if trial_resp else "")
            if "修为不足" in trial_text:
                retry_holder = {"msg": None}

                async def retry_ht():
                    retry_holder["msg"] = await self.send_and_wait_feedback_identity(
                        avatar, ".共历心劫", reply_to=status_msg.id,
                        timeout=90, return_response_msg=True, delete_after=False,
                    )
                    return retry_holder["msg"]

                success, retry_text = await self.handle_修为不足(avatar, retry_ht, cooldown_key="next_heart_trial_time")
                if not success:
                    return False
                trial_resp = retry_holder.get("msg")
                trial_text = retry_text
            cd = self.parse_wait_time(trial_text)
            if cd > 0:
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), cd))
                return False
            if self.concubine_response_indicates_active_voyage(trial_text):
                block_until = self.concubine_voyage_block_until(avatar) or add_seconds_str(now_str(), 1800)
                self.set_avatar_state(avatar, "next_heart_trial_time", block_until)
                log.info(f"Avatar [{avatar}] heart trial blocked by active voyage until {block_until}.")
                return False
            if self.heart_trial_terminal_failure(trial_text):
                await self.sync_avatar_heart_trial_cooldown_after_failure(
                    avatar, f"start terminal response: {trial_text[:80]}"
                )
                return False
            if not trial_resp or not hasattr(trial_resp, "id"):
                log.warning(f"Avatar [{avatar}] heart trial: missing .共历心劫 response message for .稳 reply target.")
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                return False
            if self.heart_trial_requires_reply_target(trial_text):
                log.warning(f"Avatar [{avatar}] heart trial: bot still requires reply target.")
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                return False
            if not self.heart_trial_round_prompt(trial_text, 1):
                log.warning(f"Avatar [{avatar}] heart trial: response did not start round 1: {trial_text[:80]}")
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                return False

            current_msg = trial_resp
            current_text = trial_text
            for round_num in range(1, 4):
                confirmed = False
                for attempt in range(1, 4):
                    if not current_msg or not hasattr(current_msg, "id"):
                        log.warning(f"Avatar [{avatar}] heart trial: missing round {round_num} reply target.")
                        self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                        return False
                    if not self.heart_trial_round_prompt(current_text, round_num) and not self.heart_trial_settled(current_text):
                        log.warning(f"Avatar [{avatar}] heart trial: round {round_num} prompt missing: {current_text[:80]}")
                        self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                        return False
                    try:
                        await self.pause_event.wait()
                        if not await wait_for_bot_activity_before_send(self, ".稳", log):
                            return False
                        if not command_send_allowed(self, ".稳", log):
                            return False
                        remember_script_send_intent(self, ".稳")
                        sent = await self.client.send_message(self.target_chat_id, ".稳", reply_to=current_msg.id)
                        remember_script_sent_message(self, sent)
                        record_command_sent(self, sent, ".稳", identity=avatar, source="auto", reply_to=current_msg.id, logger=log)
                        schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                        log.info(f"🟢 OUT [{avatar}]:\n.稳 ({round_num}/3, try {attempt}/3)")
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] heart trial: failed to send .稳 ({round_num}/3): {e}")
                        await _handle_telegram_send_protection(
                            self, ".稳", e, logger=log, identity=avatar
                        )
                        return False

                    result_msg, current_text, confirmed = await self.wait_for_heart_trial_round_result(
                        current_msg, sent, round_num, timeout_sec=90, poll_sec=3,
                    )
                    if result_msg:
                        current_msg = result_msg
                        await log_incoming_message(
                            self, f".稳 {round_num}/3 try {attempt}/3 ({avatar})",
                            current_text, msg=result_msg, logger=log,
                        )
                    if self.heart_trial_settled(current_text):
                        self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 10 * 3600))
                        return True
                    if confirmed:
                        break
                    if self.heart_trial_terminal_failure(current_text):
                        await self.sync_avatar_heart_trial_cooldown_after_failure(
                            avatar, f"round {round_num} terminal response: {current_text[:80]}"
                        )
                        return False
                    if attempt < 3:
                        await asyncio.sleep(3)
                if not confirmed:
                    log.warning(f"Avatar [{avatar}] heart trial: round {round_num} failed after 3 attempts.")
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                    return False
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 10 * 3600))
            return True

    # ============================================================
    # 身外化身：闯塔
    # ============================================================

    async def run_avatar_tower_loop(self, avatar):
        return await self.run_common_avatar_tower_loop(
            avatar,
            timeout=120,
            handle_insufficient_cultivation=True,
            require_meditation_ready=False,
            sleep_func=scheduler_sleep_seconds,
            delay_range=(0, 1800),
        )

    # ============================================================
    # 身外化身：顺序执行主循环
    # ============================================================

    async def run_all_avatars_sequential(self):
        """化身顺序执行：一个做完再下一个，按最短CD等待"""
        await self.startup_done.wait()
        while self.is_running:
            await self.pause_event.wait()
            self._avatar_loop_active = True  # 整个化身批次期间保持 True
            try:
                for avatar in self.avatars:
                    features = self.avatar_features.get(avatar, {})
                    log.info(f"Avatar [{avatar}] sequential cycle start")
                    try:
                        if self.identity_pause_seconds(avatar) > 0:
                            entry = self.identity_pause_entry(avatar)
                            log.info(f"Avatar [{avatar}] is paused: {entry.get('reason', '')}; resume at {entry.get('until', '')}.")
                            continue
                        # 被动结算可能由任意指令触发；闭关链路独立负责续上，不阻塞其他任务。
                        await self._avatar_meditation_check(avatar)
                        # 0. 每日点卯
                        if features.get("daily_checkin"): await self._avatar_daily_checkin(avatar)
                        # 0.1 无咎子每日观命/定命
                        if features.get("destiny"): await self._avatar_destiny_check(avatar)
                        # 0.2 无咎子元婴/裂缝
                        if features.get("yuanying_out"): await self._avatar_yuanying_out_check(avatar)
                        if self.identity_pause_seconds(avatar) > 0:
                            continue
                        if features.get("rift_search"): await self._avatar_rift_search_check(avatar)
                        if self.identity_pause_seconds(avatar) > 0:
                            continue
                        # 2. 野外历练由独立循环负责，避免被闭关/侍妾/阵法长流程拖慢。
                        # 3. 阵法 (星宫)
                        if features.get("formation"): await self.execute_avatar_formation(avatar)
                        elif features.get("formation_assist") and self.pending_formation_invite_msg and not self.formation_assist_in_progress:
                            await self._avatar_assist_formation(avatar)
                        # 4. 闯塔
                        if features.get("tower"): await self._avatar_tower_check(avatar)
                        # 5. 灵树灌溉
                        if features.get("spirit_tree_irrigation"): await self._avatar_spirit_tree_irrigation_check(avatar)
                        # 5. 侍妾批次：远航归来 -> 天机代卜 -> 入梦寻图 -> 共历心劫 -> 侍妾远航
                        if features.get("dream_map") or features.get("heart_trial"):
                            await self.execute_avatar_concubine_chain(avatar)
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] error: {e}")
                    log.info(f"Avatar [{avatar}] cycle complete")
            finally:
                self._avatar_loop_active = False  # 所有化身完成后才释放
            # 按最短CD等待，而非固定30分钟
            cd = await self._get_avatar_min_cd_seconds()
            log.info(f"All avatars done. Next cycle in {cd}s.")
            await asyncio.sleep(scheduler_sleep_seconds(cd, minimum=1))

    async def run_avatar_field_training_loop(self):
        """化身野外历练独立循环，按各自冷却到点执行。"""
        await self.startup_done.wait()
        await asyncio.sleep(10)
        while self.is_running:
            try:
                await self.pause_event.wait()
                due_avatars = []
                next_wait = 600
                for avatar in self.avatars:
                    if self.identity_pause_seconds(avatar) > 0:
                        continue
                    features = self.avatar_features.get(avatar, {})
                    if not features.get("training_cmd"):
                        continue
                    a_state = self.get_avatar_state(avatar)
                    next_time = a_state.get("next_field_training_time", "")
                    if next_time and is_future(next_time):
                        next_wait = min(next_wait, max(60, seconds_until(next_time)))
                    else:
                        due_avatars.append(avatar)

                if not due_avatars:
                    await asyncio.sleep(scheduler_sleep_seconds(next_wait, minimum=60))
                    continue

                for avatar in due_avatars:
                    if not self.is_running:
                        break
                    await self.pause_event.wait()
                    try:
                        await self._avatar_field_training_check(avatar)
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] field training loop error: {e}", exc_info=True)
                    await asyncio.sleep(3)

                await asyncio.sleep(5)
            except Exception as e:
                log.error(f"Avatar field training scheduler error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _avatar_daily_checkin(self, avatar):
        return await self.common_avatar_daily_checkin(
            avatar,
            daily_start_wait_func=seconds_until_daily_task_start,
        )

    def _avatar_destiny_window(self, now=None):
        now = now or datetime.now()
        start = now.replace(
            hour=DESTINY_WINDOW_START_HOUR,
            minute=DESTINY_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        end = now.replace(
            hour=DESTINY_WINDOW_END_HOUR,
            minute=DESTINY_WINDOW_END_MINUTE,
            second=59,
            microsecond=999999,
        )
        return start, end

    def _avatar_destiny_wait_seconds(self, avatar, now=None):
        if avatar != DESTINY_AVATAR:
            return None
        now = now or datetime.now()
        a_state = self.get_avatar_state(avatar)
        today = now.strftime("%Y-%m-%d")
        start, end = self._avatar_destiny_window(now)
        if a_state.get("last_destiny_date") != today:
            if now < start:
                return max(0, int((start - now).total_seconds()))
            if now <= end:
                return 0
        tomorrow = now + timedelta(days=1)
        tomorrow_start = tomorrow.replace(
            hour=DESTINY_WINDOW_START_HOUR,
            minute=DESTINY_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        return max(60, int((tomorrow_start - now).total_seconds()))

    def _avatar_destiny_pending_window(self, avatar, now=None):
        if avatar != DESTINY_AVATAR or self.dashboard_command_paused(".观命", avatar):
            return None
        now = now or datetime.now()
        a_state = self.get_avatar_state(avatar)
        today = now.strftime("%Y-%m-%d")
        start, end = self._avatar_destiny_window(now)
        if a_state.get("last_destiny_date") != today and now <= end:
            return start, end
        tomorrow = now + timedelta(days=1)
        tomorrow_start = tomorrow.replace(
            hour=DESTINY_WINDOW_START_HOUR,
            minute=DESTINY_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        tomorrow_end = tomorrow.replace(
            hour=DESTINY_WINDOW_END_HOUR,
            minute=DESTINY_WINDOW_END_MINUTE,
            second=59,
            microsecond=999999,
        )
        return tomorrow_start, tomorrow_end

    async def _avatar_destiny_check(self, avatar):
        """无咎子每日 00:10-00:20 观命，并按结果定命。"""
        if avatar != DESTINY_AVATAR:
            return
        if self.dashboard_command_paused(".观命", avatar):
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        a_state = self.get_avatar_state(avatar)
        if a_state.get("last_destiny_date") == today:
            return
        start, end = self._avatar_destiny_window(now)
        if now < start or now > end:
            return

        async with AtomicTaskContext(self, f"Destiny-{avatar}"):
            resp_text = ""
            for attempt in range(2):
                resp = await self.send_and_wait_feedback_identity(avatar, ".观命", timeout=90)
                resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
                if is_deep_meditation_settlement_response(resp_text) or "神魂正在归位" in resp_text:
                    self.mark_avatar_meditation_restart_pending(avatar, ".观命 triggered settlement")
                    if attempt == 0:
                        log.info(f"[{avatar}] .观命 triggered meditation settlement; retrying .观命 once.")
                        await asyncio.sleep(3)
                        continue
                break
            if not resp_text:
                log.warning(f"[{avatar}] .观命 未收到有效回复，保留今日重试机会。")
                return
            clean_text = resp_text.replace("**", "")
            if any(keyword in clean_text for keyword in DESTINY_OBSERVE_FAILURE_KEYWORDS):
                log.info(f"[{avatar}] .观命 返回失败/不可执行，跳过定命并保留重试机会。")
                return

            choice = "贪狼" if "贪狼" in clean_text else "太阴"
            destiny_cmd = f".定命 {choice}"
            if self.dashboard_command_paused(destiny_cmd, avatar):
                log.info(f"[{avatar}] {destiny_cmd} 已在 dashboard 暂停，跳过定命。")
            else:
                await asyncio.sleep(3)
                await self.send_and_wait_feedback_identity(avatar, destiny_cmd, timeout=90)

            self.set_avatar_state(avatar, "last_destiny_date", today)
            self.set_avatar_state(avatar, "last_destiny_time", now_str())
            self.set_avatar_state(avatar, "last_destiny_choice", choice)

    async def _avatar_meditation_check(self, avatar):
        """
        化身闭关检查（单次，原子流程）。
        整个流程必须连续执行，不被身份切换打断：
        1. .查看闭关 → 如果返回带时间格式（正在深度闭关），更新状态并返回
        2. （无咎子专属）.推命 闭关
        3. .闭关修炼 → 只要机器人有响应就继续
        4. .深度闭关
        """
        a_state = self.get_avatar_state(avatar)
        features = self.avatar_features.get(avatar, {})
        prefix = features.get("meditation_prefix", "")

        if self.ensure_meditation_guard_from_end_time(a_state):
            self.save_state()
        guard_wait = self.meditation_guard_wait_seconds_for_state(a_state)
        if guard_wait > 0:
            log.info(
                f"[{avatar}] 深度闭关保护中，剩余 {self.compact_duration_text(guard_wait)}，跳过闭关检查。"
            )
            return

        # 内存状态快速路径：如果标记为深度闭关且未到期，直接跳过（不阻塞）
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            if end_time and is_future(end_time):
                log.info(f"[{avatar}] 深度闭关中，剩余 {seconds_until(end_time)}s，跳过闭关检查。")
                return  # 不 sleep，让外层循环继续检查其他任务（历练/心劫/入梦等）
            self.mark_avatar_meditation_restart_pending(avatar, "local end time expired")
            a_state = self.get_avatar_state(avatar)

        if a_state.get("meditation_restart_mode") == "deep_only":
            if await self.restart_avatar_deep_meditation_direct(avatar, "pending force-exit restart"):
                return
            a_state = self.get_avatar_state(avatar)

        next_med = self.meditation_defer_until(a_state)
        if next_med and is_future(next_med):
            log.info(f"[{avatar}] 闭关冷却中，等待到 {next_med} 后重试。")
            return

        if features.get("destiny"):
            now_dt = datetime.now()
            today = now_dt.strftime("%Y-%m-%d")
            if a_state.get("last_destiny_date") != today:
                pending_window = self._avatar_destiny_pending_window(avatar, now_dt)
                if pending_window:
                    window_start, window_end = pending_window
                    defer_start = window_start - timedelta(seconds=DESTINY_MEDITATION_DEFER_SECONDS)
                    if defer_start <= now_dt <= window_end:
                        log.info(
                            f"[{avatar}] 观命窗口临近 {dt_to_str(window_start)} - {dt_to_str(window_end)}，"
                            "暂缓新开深度闭关。"
                        )
                        return

        # 原子流程：整个闭关流程不被身份切换打断
        async with AtomicTaskContext(self, f"Meditation-{avatar}"):
            # Step 1: .查看闭关 → 判断是否已在深度闭关中
            check_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
            check_text = getattr(check_resp, "text", "") if hasattr(check_resp, "text") else str(check_resp) if check_resp else ""

            if is_deep_meditation_settlement_response(check_text):
                log.info(f"[{avatar}] .查看闭关: received settlement summary, restarting immediately.")
            elif is_not_deep_meditation_response(check_text):
                log.info(f"[{avatar}] .查看闭关: not in deep meditation, restarting immediately.")
            # 如果返回带时间格式（正在深度闭关/预计还需），更新状态并返回
            elif is_deep_meditation_ongoing_response(check_text):
                log.info(f"[{avatar}] .查看闭关: 已在深度闭关中。")
                cd = self.parse_wait_time(check_text)
                if cd > 0:
                    self.update_avatar_states(
                        avatar,
                        self.meditation_active_state_values(add_seconds_str(now_str(), cd)),
                    )
                return

            await asyncio.sleep(3)

            # Step 2: 推命前缀（无咎子专属，在 .闭关修炼 前执行）
            if prefix:
                log.info(f"[{avatar}] 发送 {prefix} 闭关")
                await self.send_and_wait_feedback_identity(avatar, f"{prefix} 闭关")
                await asyncio.sleep(3)

            # Step 3: .闭关修炼 → 只要机器人有响应就继续
            log.info(f"[{avatar}] 发送 .闭关修炼")
            cr = await self.send_and_wait_feedback_identity(avatar, ".闭关修炼")
            cr_text = getattr(cr, "text", "") if hasattr(cr, "text") else str(cr) if cr else ""

            # 检查冷却（"需要打坐调息 X 分钟"、"冷却"、"无法立即"）
            # 闭关成功/失败都表示本次 .闭关修炼 已结算，后面必须立刻接 .深度闭关。
            if not any(k in cr_text for k in ["闭关成功", "闭关失败"]):
                retry_cd = self.defer_meditation_after_cultivation_cooldown(
                    avatar, cr_text, f"[{avatar}] .闭关修炼"
                )
                if retry_cd:
                    log.info(f"[{avatar}] 闭关冷却中 {retry_cd}s，稍后重试。")
                    return

            await asyncio.sleep(3)

            # Step 4: .深度闭关
            log.info(f"[{avatar}] 发送 .深度闭关")
            dr = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
            dr_text = getattr(dr, "text", "") if hasattr(dr, "text") else str(dr) if dr else ""
            await self.record_avatar_deep_meditation_start(avatar, dr_text)

    async def _avatar_tower_check(self, avatar):
        await self.common_avatar_tower_tick(
            avatar,
            timeout=120,
            min_hour=23,
            handle_insufficient_cultivation=True,
            require_meditation_ready=False,
        )

    async def _avatar_dream_map_check(self, avatar):
        a_state = self.get_avatar_state(avatar)
        nt = a_state.get("next_dream_map_time", "")
        if nt and is_future(nt): return
        resp = await self.send_and_wait_feedback_identity(avatar, ".入梦寻图", timeout=60)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else ""
        if "修为不足" in resp_text:
            async def rd(): return await self.send_and_wait_feedback_identity(avatar, ".入梦寻图", timeout=60)
            success, resp_text = await self.handle_修为不足(avatar, rd, cooldown_key="next_dream_map_time")
            if not success: return
            resp_text = getattr(resp_text, "text", "") if hasattr(resp_text, "text") else resp_text if isinstance(resp_text, str) else ""
        if any(k in resp_text for k in ["无碎片","碎片不足"]):
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 86400)); return
        cd = self.parse_wait_time(resp_text)
        self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), cd if cd > 0 else 8*3600))
        if cd <= 0 and not any(k in resp_text for k in ["冷却", "后再", "尚未", "无碎片", "碎片不足", "未拥有", "不足"]):
            self.mark_concubine_dream_executed(avatar)
        if resp_text and "4/4" in resp_text:
            log.info(f"[{avatar}] 入梦寻图进度 4/4，发送 .拼图")
            await asyncio.sleep(3)
            await self.send_and_wait_feedback_identity(avatar, ".拼图", timeout=60)

    async def _avatar_heart_trial_check(self, avatar):
        a_state = self.get_avatar_state(avatar)
        nt = a_state.get("next_heart_trial_time", "")
        if nt and is_future(nt): return
        # 使用消息ID获取侍妾状态消息（concubine loop 存储的是 last_concubine_status_msg_id）
        status_msg_id = self.state.get("last_concubine_status_msg_id")
        if not status_msg_id:
            # 没有缓存，主动查一次 .我的侍妾
            status_msg = await self.send_and_wait_feedback_identity(
                avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
            )
            if status_msg and hasattr(status_msg, "id"):
                self.state["last_concubine_status_msg_id"] = status_msg.id
            else:
                log.warning(f"[{avatar}] heart trial: cannot get .我的侍妾 response, skip.")
                return
        else:
            # 用缓存的ID获取消息对象
            try:
                status_msg = await self.client.get_messages(self.target_chat_id, ids=status_msg_id)
            except Exception as e:
                log.warning(f"[{avatar}] heart trial: cannot fetch status msg {status_msg_id}: {e}")
                return
        if not status_msg:
            return
        await self.execute_avatar_heart_trial(avatar, status_msg)

    def spirit_tree_irrigation_response_is_actionable(self, text):
        clean = self.clean_spirit_tree_text(text)
        return bool(
            clean
            and (
                self.spirit_tree_text_is_no_irrigation_response(clean)
                or self.spirit_tree_text_indicates_irrigation_state(clean)
                or self.spirit_tree_text_is_irrigation_cooldown(clean)
            )
        )

    async def send_spirit_tree_irrigation_once(self, identity, attempt=1, total=1, suppress_no_response_alert=False):
        sent_at_hint = now_str()
        if total > 1:
            log.info(f"[{identity}] spirit tree irrigation attempt {attempt}/{total}: sending {SPIRIT_TREE_IRRIGATION_COMMAND}.")
        resp = await self.send_and_wait_feedback_identity(
            identity,
            SPIRIT_TREE_IRRIGATION_COMMAND,
            timeout=60,
            max_retries=0 if total > 1 else 2,
            suppress_no_response_alert=suppress_no_response_alert,
        )
        cached_sent_at = self.latest_command_sent_at(identity, SPIRIT_TREE_IRRIGATION_COMMAND)
        sent_at = cached_sent_at if cached_sent_at and cached_sent_at >= sent_at_hint else sent_at_hint
        return {
            "resp": resp,
            "text": self.response_text(resp),
            "sent_at": sent_at,
        }

    async def _identity_spirit_tree_irrigation_check(self, identity):
        """身份级灵树灌溉检查（每2小时一次）。"""
        identity = str(identity or "主魂").strip() or "主魂"
        if self.identity_pause_seconds(identity) > 0:
            return
        if self.dashboard_command_paused(SPIRIT_TREE_IRRIGATION_COMMAND, identity):
            return
        a_state = self.spirit_tree_state_for_identity(identity)
        if a_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
            mature_until = a_state.get("spirit_tree_mature_until", "")
            if mature_until and is_future(mature_until):
                if not (
                    a_state.get("spirit_tree_harvested_in_mature_period")
                    or a_state.get("spirit_tree_harvest_attempted_in_mature_period")
                    or self.spirit_tree_harvest_locked(identity)
                ):
                    if identity == SPIRIT_TREE_AVATAR:
                        self.schedule_spirit_tree_harvest_once("irrigation check")
                    else:
                        self.schedule_spirit_tree_harvest_once("irrigation check", identity=identity)
                log.info(f"[{identity}] 灵树处于成熟采摘期，暂停灌溉至 {mature_until}。")
                return
            if mature_until:
                log.info(f"[{identity}] 灵树成熟期缓存到期，发送灌溉并按回复校准状态。")
        else:
            a_state = self.normalize_spirit_tree_state(identity)
        nt = self.get_spirit_tree_irrigation_time(identity)
        if nt and is_future(nt):
            remaining = seconds_until(nt)
            if remaining > SWITCH_COMMAND_LEAD_SECONDS or self.current_identity == identity:
                return
            log.info(
                f"[{identity}] spirit tree irrigation due in {remaining:.1f}s; "
                "switch lead active, sending irrigation chain now."
            )
        max_attempts = 2 if identity == "主魂" else 1
        attempts = []
        for attempt in range(1, max_attempts + 1):
            has_actionable_response = any(
                self.spirit_tree_irrigation_response_is_actionable(item.get("text", ""))
                for item in attempts
            )
            item = await self.send_spirit_tree_irrigation_once(
                identity,
                attempt=attempt,
                total=max_attempts,
                suppress_no_response_alert=max_attempts > 1 and (attempt < max_attempts or has_actionable_response),
            )
            attempts.append(item)
            resp_text = item.get("text", "")
            if self.spirit_tree_text_is_no_irrigation_response(resp_text) or self.spirit_tree_text_is_irrigation_cooldown(resp_text):
                break

        selected = None
        for item in reversed(attempts):
            if self.spirit_tree_irrigation_response_is_actionable(item.get("text", "")):
                selected = item
                break
        if selected is None and attempts:
            selected = next((item for item in reversed(attempts) if item.get("text")), attempts[-1])
        selected = selected or {"text": "", "sent_at": now_str()}
        resp_text = selected.get("text", "")
        sent_at = selected.get("sent_at", now_str())
        if self.spirit_tree_text_is_no_irrigation_response(resp_text):
            if await self.refresh_spirit_tree_status_after_no_irrigation(identity, source="irrigation response"):
                return
        if self.maybe_record_spirit_tree_passive_message(
            None,
            resp_text,
            source="irrigation response",
            identity=identity,
            irrigation_success_base_time=sent_at,
        ):
            return
        a_state = self.spirit_tree_state_for_identity(identity)
        a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
        # 处理冷却时间（从响应中解析或使用默认值）
        cd = self.parse_wait_time(resp_text)
        self.set_spirit_tree_irrigation_time(
            identity,
            add_seconds_str(now_str() if cd > 0 else sent_at, cd if cd > 0 else 2 * 3600),
        )
        self.save_state()

    async def _avatar_spirit_tree_irrigation_check(self, avatar):
        """化身灵树灌溉检查（每2小时一次）。"""
        await self._identity_spirit_tree_irrigation_check(avatar)

    def spirit_tree_next_wait_seconds(self, identity="主魂"):
        state = self.spirit_tree_state_for_identity(identity)
        waits = []
        if state.get("spirit_tree_guard_pending") or state.get("spirit_tree_invasion_status"):
            waits.append(60)
        if state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
            mature_until = state.get("spirit_tree_mature_until", "")
            if mature_until and is_future(mature_until):
                waits.append(seconds_until(mature_until))
        irrigation_time = self.get_spirit_tree_irrigation_time(identity)
        if irrigation_time and is_future(irrigation_time):
            remaining = seconds_until(irrigation_time)
            if self.current_identity != identity:
                remaining = max(1, remaining - SWITCH_COMMAND_LEAD_SECONDS)
            waits.append(remaining)
        for value in self.spirit_tree_guard_times().values():
            if value and is_future(value):
                waits.append(seconds_until(value))
        if not waits:
            return 5
        return max(1, min(int(w) for w in waits if w is not None))

    async def run_main_spirit_tree_loop(self):
        """主魂落云宗灵树循环。"""
        await self.startup_done.wait()
        while self.is_running:
            try:
                pause = self.identity_pause_seconds("主魂")
                if pause > 0:
                    await asyncio.sleep(scheduler_sleep_seconds(pause, minimum=60))
                    continue
                state = self.spirit_tree_state_for_identity("主魂")
                if state.get("spirit_tree_guard_pending") or state.get("spirit_tree_invasion_status"):
                    self.schedule_spirit_tree_guard_opportunities("main spirit tree loop")
                await self._identity_spirit_tree_irrigation_check("主魂")
            except Exception as e:
                log.error(f"Main spirit tree loop error: {e}", exc_info=True)
            wait = self.spirit_tree_next_wait_seconds("主魂")
            log.info(f"Main spirit tree loop sleeping {wait}s.")
            await asyncio.sleep(scheduler_sleep_seconds(wait, minimum=1))


    async def _avatar_field_training_check(self, avatar):
        """化身野外历练检查（单次）。无咎子先发探索前置指令，再发 .野外历练 深入"""
        await self.common_avatar_field_training_tick(
            avatar,
            handle_insufficient_cultivation=True,
        )

    async def _avatar_yuanying_out_check(self, avatar):
        """化身元婴出窍检查。无咎子目前启用，状态写入化身自己的 state。"""
        return await self.common_avatar_yuanying_out_check(avatar, require_meditation_ready=False)

    async def _avatar_rift_search_check(self, avatar):
        """化身探寻裂缝检查。虚弱结果由回复/edited 监听暂停对应身份。"""
        return await self.common_avatar_rift_search_check(
            avatar,
            RIFT_SEARCH_CD_SECONDS,
            require_meditation_ready=False,
        )

    async def _get_avatar_min_cd_seconds(self):
        """计算所有化身中最早到期的CD时间（秒），用于替代固定30分钟sleep"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        min_cd = 1800  # 兜底30分钟
        for avatar in self.avatars:
            if self.identity_pause_seconds(avatar) > 0:
                continue
            a_state = self.get_avatar_state(avatar)
            features = self.avatar_features.get(avatar, {})
            if features.get("daily_checkin") and a_state.get("last_dianmao_date") != today and seconds_until_daily_task_start(now) <= 0:
                min_cd = min(min_cd, 60)
            if features.get("destiny") and not self.dashboard_command_paused(".观命", avatar):
                destiny_wait = self._avatar_destiny_wait_seconds(avatar, now)
                if destiny_wait is not None:
                    min_cd = min(min_cd, destiny_wait)
            # 闭关CD
            next_med = self.meditation_defer_until(a_state)
            if next_med and is_future(next_med):
                min_cd = min(min_cd, seconds_until(next_med))
            elif self.avatar_meditation_needs_attention(avatar):
                min_cd = min(min_cd, 60)
            elif a_state.get("in_deep_meditation"):
                end = a_state.get("deep_meditation_end_time", "")
                if end and is_future(end):
                    min_cd = min(min_cd, seconds_until(end))
            # 闯塔CD（23点后+今天没做过）
            if features.get("tower") and a_state.get("last_tower_date") != today and now.hour >= 23:
                min_cd = min(min_cd, 60)
            # 野外历练CD
            ft = a_state.get("next_field_training_time", "")
            if ft and is_future(ft):
                min_cd = min(min_cd, seconds_until(ft))
            for key in ("next_yuanying_out_time", "next_rift_search_time"):
                if not (
                    (key == "next_yuanying_out_time" and features.get("yuanying_out"))
                    or (key == "next_rift_search_time" and features.get("rift_search"))
                ):
                    continue
                if self.state_time_command_paused(key, avatar):
                    continue
                value = a_state.get(key, "")
                if value and is_future(value):
                    min_cd = min(min_cd, seconds_until(value))
                else:
                    min_cd = min(min_cd, 60)
            # 阵法CD/重试/强行出关
            if features.get("formation"):
                for key in ("next_formation_time", "next_formation_retry_time", "next_force_exit_time"):
                    value = a_state.get(key, "")
                    if value and is_future(value):
                        min_cd = min(min_cd, seconds_until(value))
            elif features.get("formation_assist"):
                value = a_state.get("next_force_exit_time", "")
                if value and is_future(value):
                    min_cd = min(min_cd, seconds_until(value))
            # 侍妾批次CD
            if features.get("dream_map") or features.get("heart_trial"):
                if self.concubine_voyage_auto_start_enabled(avatar) and not self.dashboard_command_paused(".侍妾远航 冒险", avatar):
                    bound_time = self.latest_concubine_chain_time(avatar)
                    if bound_time and is_future(bound_time):
                        min_cd = min(min_cd, seconds_until(bound_time))
                else:
                    dm = a_state.get("next_dream_map_time", "")
                    if dm and is_future(dm):
                        min_cd = min(min_cd, seconds_until(dm))
            # 心劫CD
            if features.get("heart_trial"):
                ht = a_state.get("next_heart_trial_time", "")
                if ht and is_future(ht):
                    min_cd = min(min_cd, seconds_until(ht))
            # 侍妾远航CD（非绑定路径兜底）
            if (
                self.concubine_voyage_enabled(avatar)
                and not features.get("dream_map")
                and not self.dashboard_command_paused(".侍妾远航 冒险", avatar)
            ):
                voyage = a_state.get("next_concubine_voyage_time", "")
                if voyage and is_future(voyage) and (
                    self.concubine_voyage_auto_start_enabled(avatar)
                    or a_state.get("concubine_voyage_active")
                ):
                    min_cd = min(min_cd, seconds_until(voyage))
            # 灵树灌溉CD
            if features.get("spirit_tree_irrigation"):
                tree_state = self.spirit_tree_state_for_identity(avatar)
                if tree_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
                    mature_until = tree_state.get("spirit_tree_mature_until", "")
                    if mature_until and is_future(mature_until):
                        min_cd = min(min_cd, seconds_until(mature_until))
                        continue
                st = self.get_spirit_tree_irrigation_time(avatar)
                if st and is_future(st):
                    remaining = seconds_until(st)
                    if self.current_identity != avatar:
                        remaining = max(1, remaining - SWITCH_COMMAND_LEAD_SECONDS)
                    min_cd = min(min_cd, remaining)
                else:
                    min_cd = min(min_cd, 60)
        return max(min_cd, 1)
    async def run_cultivation_loop(self):
        """
        主循环入口。启动所有子任务。

        做的事情：
          1. 延迟 10 秒（给客户端足够时间初始化）
          2. 执行启动同步（startup_sync）：检查天阶状态、闭关状态
          3. 启动所有独立循环作为后台任务
          4. 保持主循环运行

        启动的独立循环（每个都是独立的 asyncio.Task）：
          - run_daily_tasks: 每日任务
          - run_cloud_stairs_loop: 云阶问心
          - run_nine_heaven_wind_loop: 九天罡风
          - run_yuanying_out_loop: 元婴出窍
          - run_rift_search_loop: 探寻裂缝
          - run_treasure_touch_loop: 抚摸法宝
          - run_meditation_timer: 深度闭关
          - run_concubine_loop: 侍妾神通（继承自 ConcubineMixin）
          - run_field_training_loop: 田野修炼（继承自 CommonCommandMixin）
          - run_sect_war_loop: 宗门战（继承自 CommonCommandMixin）
        """
        await asyncio.sleep(10)

        # ---- 启动同步 ----
        # 启动后智能同步一次：如果记录的时间不是未来，说明已过时，需要刷新。
        # 这是为了在脚本重启后能正确恢复各循环的状态。
        async def startup_sync():
            try:
                await asyncio.sleep(5)
                log.info("Startup Sync: Smart check for stale data...")
                if self.identity_pause_seconds("主魂") > 0:
                    log.info(
                        "Startup Sync: main soul is paused; skipping main-soul startup checks "
                        "and releasing avatar loops."
                    )
                    self.startup_done.set()
                    return

                # 1) 云阶状态同步
                if not self.lingxiao_enabled:
                    log.info("Startup Sync: Lingxiao cloud stairs disabled for current sect.")
                else:
                    next_stairs = self.restore_cloud_stairs_time_from_last()
                    if next_stairs and is_future(next_stairs):
                        log.info(f"Startup Sync: Cloud stairs CD present. Skipping .天阶状态. Next: {next_stairs}")
                    else:
                        log.info(
                            "Startup Sync: Skipping .天阶状态 before cloud-stairs climb; "
                            ".登天阶 response will refresh progress/CD."
                        )

                # 2) 闭关状态同步
                end_med = self.state.get("deep_meditation_end_time", "")
                if end_med and is_future(end_med):
                    log.info(f"Startup Sync: Local meditation time valid ({end_med}). Skipping check.")
                else:
                    log.info("Startup Sync: Querying meditation via .查看闭关...")
                    resp_med = await self.send_and_wait_feedback(".查看闭关")
                    if resp_med:
                        cd_med = self.parse_wait_time(resp_med)
                        if cd_med > 0:
                            self.state.update(
                                self.meditation_active_state_values(
                                    add_seconds_str(now_str(), cd_med),
                                    clear_restart=False,
                                )
                            )
                            log.info(f"Startup Sync: Meditation end time found: {self.state['deep_meditation_end_time']}")
                        elif is_deep_meditation_settlement_response(resp_med) or is_not_deep_meditation_response(resp_med):
                            self.state["in_deep_meditation"] = False
                            self.state["deep_meditation_end_time"] = ""
                            self.state["deep_meditation_guard_until"] = ""
                            log.info("Startup Sync: Meditation inactive or settlement detected.")
                        elif is_deep_meditation_ongoing_response(resp_med):
                            self.state["in_deep_meditation"] = True
                            log.info("Startup Sync: Ongoing meditation detected.")

                # 3) 恢复化身助阵后的强行出关计时器
                for avatar_name in self.avatars:
                    a_state = self.get_avatar_state(avatar_name)
                    force_exit_at = a_state.get("next_force_exit_time", "")
                    if force_exit_at and is_future(force_exit_at):
                        force_delay = seconds_until(force_exit_at)
                        log.info(
                            f"Startup Sync: Restoring avatar [{avatar_name}] force-exit "
                            f"at {force_exit_at} ({int(force_delay)}s)."
                        )
                        asyncio.create_task(self.delayed_avatar_force_exit(avatar_name, force_delay))
                    elif force_exit_at:
                        active_until = a_state.get("formation_active_until", "")
                        if active_until and is_future(active_until):
                            log.warning(
                                f"Startup Sync: avatar [{avatar_name}] force-exit overdue "
                                f"but formation bonus active. Triggering now."
                            )
                            self.set_avatar_state(avatar_name, "next_force_exit_time", "")
                            asyncio.create_task(self.delayed_avatar_force_exit(avatar_name, 0))
                        else:
                            log.info(
                                f"Startup Sync: clearing expired avatar [{avatar_name}] "
                                f"force-exit time {force_exit_at}."
                            )
                            self.set_avatar_state(avatar_name, "next_force_exit_time", "")

                self.save_state()
                self.startup_done.set()  # 释放所有等待的循环
            except Exception as e:
                log.error(f"Startup Sync FAILED: {e}", exc_info=True)
            finally:
                self.startup_done.set()  # 确保无论如何都释放循环
            log.info("Startup Sync: Finished. All loops released.")

        # 启动同步任务（不 await，让它在后台执行）
        asyncio.create_task(startup_sync())
        # 启动日志修剪任务（定期清理旧日志）
        asyncio.create_task(periodic_log_prune(LOG_FILE))
        asyncio.create_task(self.run_health_watchdog_loop())

        # ---- 启动所有独立循环 ----
        # 每个循环都是独立的 asyncio.Task，通过 startup_done.wait() 等待同步完成

        # 每日任务循环（闯塔、点卯、传功）
        asyncio.create_task(self.run_daily_tasks())

        if self.lingxiao_enabled:
            # 云阶问心循环（凌霄宫）
            asyncio.create_task(self.run_cloud_stairs_loop())
            # 九天罡风循环（独立于登天阶循环）
            asyncio.create_task(self.run_nine_heaven_wind_loop())
        else:
            log.info("Lingxiao cloud stairs / wind loops disabled for current sect.")

        # 元婴期能力循环（出窍+归窍）
        asyncio.create_task(self.run_yuanying_out_loop())
        asyncio.create_task(self.run_rift_search_loop())

        # 抚摸法宝循环
        asyncio.create_task(self.run_treasure_touch_loop())
        asyncio.create_task(self.run_nurture_spirit_loop())

        # 闭关循环（内部会检查深度闭关状态）
        asyncio.create_task(self.run_meditation_timer())

        # 侍妾神通循环（继承自 ConcubineMixin）
        asyncio.create_task(self.run_concubine_loop())
        asyncio.create_task(self.run_fishing_loop("主魂", initial_delay=20))
        asyncio.create_task(self.run_fishing_auto_loop(initial_delay=25))

        # 通用固定冷却指令循环（继承自 CommonCommandMixin）
        asyncio.create_task(self.run_field_training_loop())
        asyncio.create_task(self.run_sect_war_loop())
        asyncio.create_task(self.run_custom_command_loop())
        asyncio.create_task(self.run_daily_reward_summary_loop(initial_delay=40))
        if not self.lingxiao_enabled:
            asyncio.create_task(self.run_main_spirit_tree_loop())

        # ---- 化身系统 ----
        asyncio.create_task(self.run_all_avatars_sequential())
        asyncio.create_task(self.run_avatar_field_training_loop())
        asyncio.create_task(self.run_star_gazing_loop())
        for i, avatar_name in enumerate(self.avatars):
            asyncio.create_task(self.run_fishing_loop(avatar_name, initial_delay=30 + i * 10))
            if avatar_name in STAR_ATTRACTION_AVATARS:
                asyncio.create_task(self.run_avatar_star_attraction_loop(avatar_name, initial_delay=i * 10))
        if YINLUO_IDENTITY in self.avatars:
            asyncio.create_task(self.run_yinluo_loop(YINLUO_IDENTITY, initial_delay=45))


        # ---- 保持主循环运行 ----
        # 主 coroutine 不能退出，否则子任务也会被取消
        while self.is_running:
            await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # 启动入口
    # ------------------------------------------------------------------

    async def start(self):
        """
        启动 Telegram 客户端并绑定事件处理器。

        流程：
          1. 启动客户端（登录/恢复 session）
          2. 获取对话列表（确保目标聊天被缓存）
          3. 获取自身用户信息
          4. 注册消息处理器
          5. 进入主循环

        注册的事件处理器：
          - NewMessage: 处理游戏消息（handle_game_response）
          - MessageEdited: 记录编辑过的消息（仅日志）
        """
        await self.client.start()
        # 热身：获取最近的对话列表，确保缓存了目标聊天 ID
        await self.client.get_dialogs(limit=20)
        # 获取当前账号信息（用户名、first_name 等）
        self.my_info = await self.client.get_me()
        log.info(f"Main Login: {self.my_info.first_name}")

        # 注册新消息事件（所有游戏消息走这里）
        @self.client.on(events.NewMessage(chats=self.target_chat_id))
        async def handler(event):
            await self.handle_game_response(event)

        # 注册消息编辑事件（记录消息编辑，用于调试）
        @self.client.on(events.MessageEdited(chats=self.target_chat_id))
        async def edit_handler(event):
            await log_edited_message_if_needed(self, event)
            try:
                msg = event.message
                text = msg.text or ""
                sender = await event.get_sender()
                if is_game_bot_sender(self, sender):
                    record_game_bot_activity(self, sender, log)
                    record_star_gazing_event("main", msg, text, sender=sender, is_edited=True, logger=log)
                    self.record_star_gazing_final_report_if_needed(msg, text, source="edited message")
                    self.record_star_shift_attempt_if_needed(msg, text, source="edited message")
                    self.maybe_record_daily_reward_from_edited_message(msg, text, source="edited message")
                    # 编辑后出现元婴遁逃·虚弱 → 立刻告警并停止脚本（防漏检补丁）
                    if self.is_rift_weakness_response(text) and is_edited_message_for_current_account(self, msg, text):
                        identity = tracked_command_identity_for_reply(self, msg) or getattr(self, "current_identity", "主魂")
                        log.critical(f"Rift weakness DETECTED in edited message for [{identity}].\n{text}")
                        await self.stop_for_rift_weakness(text, identity=identity, msg=msg)
                        return
                    manual_reply = is_reply_to_manual_command(self, msg)
                    manual_processed = await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                    if not manual_reply or not manual_processed:
                        self.maybe_record_spirit_tree_passive_message(msg, text, source="edited message")
                    # 编辑消息也能触发 feedback_events（bot 通过编辑回复指令）
                    is_matched = match_pending_feedback_by_reply(
                        self, msg, text, self.is_loose_feedback_candidate, log, label="[EDITED-FEEDBACK]"
                    )
                    if not is_matched:
                        is_matched = match_pending_feedback_by_message_id(
                            self, msg, text, self.is_loose_feedback_candidate, log, label="[EDITED-FEEDBACK]"
                        )
                    if not is_matched:
                        is_matched = match_pending_edited_feedback(
                            self, msg, text, self.is_loose_feedback_candidate, log, id_window=20
                        )
            except Exception as e:
                log.error(f"Edited message handler error: {e}")

        # 进入主循环（会阻塞直到脚本停止）
        await self.run_cultivation_loop()





        # ==================== 从星宫移植的方法 ====================

    def active_star_gazing_target_dt(self, now=None):
            return self.common_active_star_gazing_target_dt(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
                monitor_lead_seconds=STAR_GAZING_MONITOR_LEAD_SECONDS,
                shift_lead_seconds=STAR_GAZING_SHIFT_LEAD_SECONDS,
            )

    def next_star_gazing_window_start_dt(self, now=None):
            return self.common_next_star_gazing_window_start_dt(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
                monitor_lead_seconds=STAR_GAZING_MONITOR_LEAD_SECONDS,
                shift_lead_seconds=STAR_GAZING_SHIFT_LEAD_SECONDS,
            )

    def star_observatory_needs_calm(self, text):
            return self.common_star_observatory_needs_calm(text)

    def star_gazing_sent_on_date(self, date_str=None):
            return self.common_star_gazing_sent_on_date(
                date_str or datetime.now().strftime("%Y-%m-%d")
            )

    def star_gazing_schedule_plan(self, now, manifest_dt):
            return self.common_star_gazing_schedule_plan(
                now,
                manifest_dt,
                command_lead_seconds=STAR_GAZING_COMMAND_LEAD_SECONDS,
            )

    def star_gazing_send_dt(self, target_dt):
            return self.common_star_gazing_send_dt(
                target_dt,
                command_lead_seconds=STAR_GAZING_COMMAND_LEAD_SECONDS,
            )

    def star_gazing_target_for_opportunity(self, now=None):
            return self.common_star_gazing_target_for_opportunity(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
            )

    def star_gazing_manifest_for_notice(self, now=None):
            return self.common_star_gazing_manifest_for_notice(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
            )

    def daily_star_gazing_fallback_dt(self, now=None):
            return self.common_daily_star_gazing_fallback_dt(
                now or datetime.now(),
                hour=STAR_GAZING_DAILY_FALLBACK_HOUR,
                minute=STAR_GAZING_DAILY_FALLBACK_MINUTE,
            )

    def has_pending_star_gazing_action(self):
            """检查是否有排期中的观星或改换星移操作（任何一项在未来有效）。"""
            return self.common_has_pending_star_gazing_action()

    def clear_pending_star_gazing_schedule(self):
            """清除所有排期中的观星数据。"""
            self.common_clear_pending_star_gazing_schedule()

    def clear_star_gazing_round_claim(self):
            """清除账号级观星轮次占用。"""
            self.common_clear_star_gazing_round_claim()

    def clear_stale_star_gazing_claim_before_manifest(self, manifest_dt, sender_info="", text_preview=""):
            """Clear an old claimed .观星 round before scheduling the current manifest."""
            return self.common_clear_stale_star_gazing_claim_before_manifest(
                manifest_dt,
                sender_info=sender_info,
                text_preview=text_preview,
                logger=log,
            )

    def star_gazing_claim_matches(self, avatar, manifest_dt):
            """确认当前任务仍是本账号在该显化轮次被指派的唯一身份。"""
            return self.common_star_gazing_claim_matches(avatar, manifest_dt)

    def claimed_star_gazing_pending_due(self, avatar, now=None):
            """Return true when a claimed .观星 send is due and may surface as a passive result."""
            pending = (
                self.state.get("pending_star_gazing_scheduled_time", "")
                or self.state.get("pending_star_gazing_target_time", "")
            )
            return self.common_claimed_star_gazing_pending_due(avatar, pending, now)

    def maybe_record_passive_claimed_star_gazing_result(self, avatar, manifest_dt, gazing_date, msg):
            """Treat a passive 星盘显化 message as the result for a due claimed .观星 command."""
            if not avatar or self.get_avatar_state(avatar).get("last_gazing_date") == gazing_date:
                return False
            if not self.claimed_star_gazing_pending_due(avatar):
                return False

            self.set_avatar_state(avatar, "last_gazing_date", gazing_date)
            self.set_avatar_state(avatar, "last_gazing_time", now_str())
            self.state["last_gazing_date"] = gazing_date
            self.state["last_gazing_time"] = now_str()
            self.common_mark_star_gazing_round_assigned(
                manifest_dt,
                avatar,
                source="passive .观星 result",
                logger=log,
            )
            self.clear_pending_star_gazing_schedule()
            self.state["next_star_gazing_time"] = ""
            self.save_state()

            msg_id = getattr(msg, "id", 0)
            log.info(
                f"Star gazing [{avatar}]: passive .观星 result observed for "
                f"{dt_to_str(manifest_dt)}; marked {gazing_date}."
            )
            if msg_id and manifest_dt and self.get_avatar_state(avatar).get("last_star_shift_date") != gazing_date:
                asyncio.create_task(self.avatar_schedule_star_shift(avatar, msg_id, manifest_dt, gazing_date))
            return True

    def choose_star_gazing_avatar_for_today(self, today):
            """按轮换顺序选择今天尚未观星的一个化身。"""
            avatars = STAR_GAZING_ROTATING_AVATARS
            if not avatars:
                return None, 0
            start_idx = self.state.get("star_gazing_avatar_index", 0) % len(avatars)
            for offset in range(len(avatars)):
                idx = (start_idx + offset) % len(avatars)
                avatar = avatars[idx]
                if self.get_avatar_state(avatar).get("last_gazing_date") != today:
                    self.state["star_gazing_avatar_index"] = (idx + 1) % len(avatars)
                    return avatar, idx
            return None, start_idx

    def clear_pending_star_shift(self):
            """清除所有排期中的改换星移数据（同时也会清除观星排期，因为两者联动）。"""
            self.state["pending_star_shift_date"] = ""
            self.state["pending_star_shift_target_time"] = ""
            self.state["pending_star_shift_msg_id"] = 0
            self.state["pending_star_gazing_time"] = ""
            self.clear_pending_star_gazing_schedule()

    def next_star_manifest_dt(self, now=None):
            return self.common_next_star_manifest_dt(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
            )

    def star_shift_done_today(self, today=None):
            return self.common_star_shift_done_today(today or datetime.now().strftime("%Y-%m-%d"))

    def pending_daily_star_gazing_fallback_dt(self, now=None):
            return self.common_pending_daily_star_gazing_fallback_dt(now or datetime.now())

    async def maybe_run_daily_star_gazing_fallback(self, now=None):
            """
            检查并执行每日备用观星（23:59 兜底）。
            如果当天一直没有观星机会，在 23:59 发送一次 .观星。

            返回 True 表示执行了操作（包括无响应的情况），False 表示未到时间或条件不满足。
            """
            now = now or datetime.now()
            fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)
            if not fallback_dt or now < fallback_dt:
                return False

            # 加锁执行，防止并发冲突
            async with self.star_gazing_lock:
                now = datetime.now()
                fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)
                if not fallback_dt or now < fallback_dt:
                    return False

                today = now.strftime("%Y-%m-%d")
                self.state["last_star_gazing_fallback_date"] = today
                self.save_state()

                if self.dashboard_command_paused(".观星", "主魂"):
                    log.info("Star gazing fallback: .观星 paused by dashboard; skipping 23:59 fallback.")
                    self.clear_pending_star_gazing_schedule()
                    self.clear_star_gazing_round_claim()
                    self.save_state()
                    return True

                log.info("Star gazing fallback: no .观星 today; sending .观星 at 23:59.")
                self.active_atomic_task = asyncio.current_task()
                log.info("🔒 [ATOMIC LOCK] Acquired by StarGazingFallback")
                try:
                    resp_msg = await self.send_and_wait_feedback_identity(
                        "主魂",
                        ".观星",
                        timeout=45,
                        max_retries=0,
                        return_response_msg=True,
                        delete_after=False,
                        force_identity_check=True,
                        suppress_no_response_alert=True,
                    )
                    if not resp_msg:
                        self.save_state()
                        return True

                    resp_text = (resp_msg.text or "")
                    self.state["last_gazing_date"] = today
                    self.state["last_gazing_time"] = now_str()

                    if self.star_gazing_good_opportunity(resp_text):
                        # 调度改换星移：由于是在 23:59 兜底，目标显化时间强制绑定在次日 00:00:00 窗口
                        tomorrow = now + timedelta(days=1)
                        target_dt = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
                        target_day = target_dt.strftime("%Y-%m-%d")
                        if self.star_gazing_final_report_seen(target_dt):
                            log.info(
                                f"Star gazing fallback: final report already seen for {dt_to_str(target_dt)}; "
                                "not scheduling .改换星移."
                            )
                            self.save_state()
                            return True
                        if not self.star_shift_done_today(target_day):
                            self.state["pending_star_shift_date"] = target_day
                            self.state["pending_star_shift_target_time"] = dt_to_str(target_dt)
                            self.state["pending_star_shift_msg_id"] = resp_msg.id
                            self.save_state()
                            log.info(
                                f"Star gazing fallback: GOOD result detected; "
                                f"scheduling .改换星移 for manifest {dt_to_str(target_dt)}."
                            )
                            self.star_shift_task = asyncio.create_task(
                                self.schedule_star_shift(resp_msg.id, target_dt, target_day)
                            )
                        else:
                            log.info("Star gazing fallback: GOOD result but shift already done today.")
                    else:
                        log.info("Star gazing fallback: .观星 result did not contain GOOD keyword.")

                    self.save_state()
                    return True
                finally:
                    if self.active_atomic_task == asyncio.current_task():
                        self.active_atomic_task = None
                        log.info("🔓 [ATOMIC LOCK] Released by StarGazingFallback")

    def next_star_manifest_dt(self, now=None):
            return self.common_next_star_manifest_dt(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
            )

    def star_shift_done_today(self, today=None):
            return self.common_star_shift_done_today(today or datetime.now().strftime("%Y-%m-%d"))

    def pending_daily_star_gazing_fallback_dt(self, now=None):
            return self.common_pending_daily_star_gazing_fallback_dt(now or datetime.now())
    def star_gazing_good_opportunity(self, text):
        return self.common_star_gazing_good_opportunity(text, STAR_GAZING_GOOD_KEYWORDS)

    def star_gazing_manifest_fate_type(self, text):
            return self.common_star_gazing_manifest_fate_type(text)

    def star_gazing_pending_fate_type(self, text):
            return self.common_star_gazing_pending_fate_type(text, STAR_GAZING_GOOD_KEYWORDS)

    def is_star_gazing_final_report(self, text):
            return self.common_is_star_gazing_final_report(text)

    def current_star_report_manifest_dt(self, now=None):
            return self.common_current_star_report_manifest_dt(
                now or datetime.now(),
                interval_hours=STAR_GAZING_INTERVAL_HOURS,
            )

    def star_gazing_final_report_seen(self, target_dt):
            return self.common_star_gazing_final_report_seen(target_dt)

    def record_star_shift_attempt_if_needed(self, msg, text, source="new message"):
            return self.common_record_star_shift_attempt_message(
                msg,
                text,
                STAR_GAZING_SHIFT_TARGET,
                source=source,
                logger=log,
            )

    def record_star_gazing_final_report_if_needed(self, msg, text, source="new message"):
            if not self.is_star_gazing_final_report(text):
                return False
            manifest_dt = self.current_star_report_manifest_dt()
            manifest_key = dt_to_str(manifest_dt)
            changed = self.state.get("last_star_gazing_report_manifest_time", "") != manifest_key
            self.state["last_star_gazing_report_manifest_time"] = manifest_key
            self.state["last_star_gazing_report_time"] = now_str()

            cancelled = False
            if self.state.get("pending_star_shift_target_time", "") == manifest_key:
                self.clear_pending_star_shift()
                cancelled = True
            elif self.state.get("pending_star_gazing_manifest_time", "") == manifest_key:
                self.clear_pending_star_gazing_schedule()
                self.clear_star_gazing_round_claim()
                cancelled = True
            if cancelled and hasattr(self, "star_shift_task") and self.star_shift_task and not self.star_shift_task.done():
                self.star_shift_task.cancel()
            if cancelled and hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
                self.star_gazing_task.cancel()
            self.save_state()
            if changed or cancelled:
                msg_id = getattr(msg, "id", "")
                log.info(
                    f"Star gazing final report seen for {manifest_key} ({source}, msg {msg_id}); "
                    f"{'cancelled pending action' if cancelled else 'marked round settled'}."
                )
            return True


    async def schedule_star_shift(self, reply_msg_id, target_dt, gazing_date=None):
            """
            调度 .改换星移 指令的发送。
            核心策略：在星盘显现后的配置窗口内发送，给机器人拥堵队列留出最终快报前的处理时间。

            流程:
              1. 在 STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS 内选择本次发送时间
              2. 根据 STAR_GAZING_SHIFT_REPEAT_COUNT 确定发送次数（目前为 1 次）
              3. 在每次发送前检查是否已经完成（可能被其他协程提前做了）
              4. 如果错过了所有发送窗口，记录警告并清理状态

            参数:
                reply_msg_id: 回复的 .观星 消息 ID（告诉游戏是对哪个观星结果的回应）。
                target_dt: 目标显化时间。
                gazing_date: 观星日期（用于判断是否已完成）。
            """
            self.active_atomic_task = asyncio.current_task()
            log.info("🔒 [ATOMIC LOCK] Acquired by StarShift")
            try:
                today = gazing_date or target_dt.strftime("%Y-%m-%d")
                fate_type = self.state.get("pending_star_gazing_fate_type", "")
                shift_dt = star_gazing_shift_dt(target_dt, fate_type=fate_type, logger=log)
                send_times = [
                    shift_dt + timedelta(seconds=i * STAR_GAZING_SHIFT_REPEAT_INTERVAL_SECONDS)
                    for i in range(STAR_GAZING_SHIFT_REPEAT_COUNT)
                ]
                last_send_dt = send_times[-1]

                # 如果今天已经完成改换星移，直接清除排期
                if self.star_shift_done_today(today):
                    self.clear_pending_star_shift()
                    self.save_state()
                    return
                if self.star_gazing_final_report_seen(target_dt):
                    log.info(
                        f"Star gazing: final report already seen for {dt_to_str(target_dt)}; "
                        "skipping pending .改换星移."
                    )
                    self.clear_pending_star_shift()
                    self.save_state()
                    return

                # 如果当前时间已超过最后一次发送时间，说明错过了窗口
                if datetime.now() > last_send_dt + timedelta(seconds=STAR_GAZING_SHIFT_GRACE_SECONDS):
                    log.warning(
                        f"Star gazing: missed shift repeat window for {dt_to_str(target_dt)}; "
                        f"clearing pending shift."
                    )
                    self.clear_pending_star_shift()
                    self.save_state()
                    return

                command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
                if self.dashboard_command_paused(command, "主魂"):
                    log.info(f"Star gazing: {command} paused by dashboard; clearing pending shift.")
                    self.clear_pending_star_shift()
                    self.save_state()
                    return
                sent_any = False
                for idx, send_dt in enumerate(send_times, 1):
                    wait_sec = (send_dt - datetime.now()).total_seconds()
                    if wait_sec > 0:
                        log.info(
                            f"Star gazing: waiting {int(wait_sec)}s for shift repeat "
                            f"{idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} at {dt_to_str(send_dt)}."
                        )
                        await asyncio.sleep(wait_sec)
                    # 等待期间可能被其他协程完成了
                    if self.star_shift_done_today(today):
                        break
                    if self.star_gazing_final_report_seen(target_dt):
                        log.info(
                            f"Star gazing: final report arrived for {dt_to_str(target_dt)} while waiting; "
                            "skipping .改换星移."
                        )
                        self.clear_pending_star_shift()
                        self.save_state()
                        return
                    if datetime.now() > send_dt + timedelta(seconds=2):
                        log.warning(
                            f"Star gazing: skipped expired shift repeat "
                            f"{idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} scheduled at {dt_to_str(send_dt)}."
                        )
                        continue
                    if not self.common_mark_star_shift_attempt(
                        "主魂",
                        today,
                        source="scheduled dispatch",
                        logger=log,
                    ):
                        break
                    self.save_state()
                    log.info(
                        f"Star gazing: sending {command} repeat {idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} "
                        f"as reply to .观星 result {reply_msg_id}."
                    )
                    sent_msg = await self.send_and_wait_feedback_identity(
                        "主魂",
                        command,
                        timeout=10,
                        reply_to=reply_msg_id,
                        suppress_no_response_alert=True,
                    )
                    if sent_msg:
                        sent_any = True

                if sent_any:
                    self.state["last_star_shift_date"] = today
                    self.state["last_star_shift_time"] = now_str()
                self.clear_pending_star_shift()
                self.save_state()
            finally:
                if self.active_atomic_task == asyncio.current_task():
                    self.active_atomic_task = None
                    log.info("🔓 [ATOMIC LOCK] Released by StarShift")

    async def avatar_schedule_star_shift(self, avatar, reply_msg_id, target_dt, gazing_date=None):
            """
            化身版改换星移调度：等待到指定时间后，使用化身身份发送 .改换星移。
            与 schedule_star_shift 类似，但通过 send_and_wait_feedback_identity 切换化身发送。

            参数:
                avatar: 化身名称（如 "厚土"）。
                reply_msg_id: 回复的 .观星 消息 ID。
                target_dt: 目标显化时间。
                gazing_date: 观星日期。
            """
            today = gazing_date or target_dt.strftime("%Y-%m-%d")
            fate_type = self.state.get("pending_star_gazing_fate_type", "")
            shift_dt = star_gazing_shift_dt(target_dt, fate_type=fate_type, logger=log)
            if self.get_avatar_state(avatar).get("last_star_shift_date") == today:
                return
            if self.star_gazing_final_report_seen(target_dt):
                log.info(
                    f"Star gazing [{avatar}]: final report already seen for {dt_to_str(target_dt)}; "
                    "skipping .改换星移."
                )
                return
            if datetime.now() > shift_dt + timedelta(seconds=STAR_GAZING_SHIFT_GRACE_SECONDS):
                return

            wait_sec = (shift_dt - datetime.now()).total_seconds()
            if wait_sec > 0:
                log.info(f"Star gazing [{avatar}]: waiting {int(wait_sec)}s for .改换星移 at {dt_to_str(shift_dt)}.")
                await asyncio.sleep(wait_sec)

            if self.get_avatar_state(avatar).get("last_star_shift_date") == today:
                return
            if not self.star_gazing_claim_matches(avatar, target_dt):
                log.info(
                    f"Star gazing [{avatar}]: pending shift for {dt_to_str(target_dt)} "
                    "no longer owns the round; skipping .改换星移."
                )
                return
            if self.star_gazing_final_report_seen(target_dt):
                log.info(
                    f"Star gazing [{avatar}]: final report arrived for {dt_to_str(target_dt)}; "
                    "skipping .改换星移."
                )
                return

            self.active_atomic_task = asyncio.current_task()
            log.info(f"🔒 [ATOMIC LOCK] Acquired by AvatarStarShift-{avatar}")
            try:
                command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
                if self.dashboard_command_paused(command, avatar):
                    log.info(f"Star gazing [{avatar}]: {command} paused by dashboard; skipping .改换星移.")
                    self.clear_pending_star_shift()
                    self.save_state()
                    return
                if not self.common_mark_star_shift_attempt(
                    avatar,
                    today,
                    source="avatar scheduled dispatch",
                    logger=log,
                ):
                    return
                self.save_state()
                log.info(f"Star gazing [{avatar}]: sending {command} as reply to .观星 result {reply_msg_id}.")
                sent_msg = await self.send_and_wait_feedback_identity(
                    avatar,
                    command,
                    reply_to=reply_msg_id,
                    suppress_no_response_alert=True,
                )
                if sent_msg:
                    self.set_avatar_state(avatar, "last_star_shift_date", today)
                    self.set_avatar_state(avatar, "last_star_shift_time", now_str())
                    log.info(f"Star gazing [{avatar}]: .改换星移 sent successfully.")
                else:
                    log.info(
                        f"Star gazing [{avatar}]: .改换星移 has no confirmed feedback; "
                        "possible control-field/no-response window."
                    )
            finally:
                if self.active_atomic_task == asyncio.current_task():
                    self.active_atomic_task = None
                    log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarShift-{avatar}")

    async def schedule_star_gazing_simple(self, send_dt, immediate_shift=False, avatar=None, manifest_dt=None, gazing_date=None):
            """
            在指定时间发送 .观星，然后根据结果决定是否触发 .改换星移。
            这是简化版调度，用于显化事件触发的情况。

            参数:
                send_dt: 发送 .观星 的时间。
                immediate_shift: 若为 True，收到 Good 结果后立刻发送 .改换星移（当前窗口活跃模式）。
                                若为 False，排期到下一个显化窗口后发送（常规模式）。
                avatar: 轮换化身名称（如 "厚土"）。若为 None 则使用主魂身份。

            流程:
                1. 等待到发送时间。
                2. 检查今天是否已观星（可能被其他协程提前做了）。
                3. 发送 .观星。
                4. 如果结果是 Good：
                   - immediate_shift=True: 立刻回复 .改换星移（抢占当前窗口）
                   - immediate_shift=False: 排期到下一个显化窗口后发送
            """
            now = datetime.now()
            wait_sec = (send_dt - now).total_seconds()
            if wait_sec > 0:
                who = avatar or "主魂"
                log.info(
                    f"Star gazing [{who}]: waiting {int(wait_sec)}s to send .观星 at {dt_to_str(send_dt)}."
                )
                await self.sleep_then_prepare_time_critical_identity(
                    send_dt,
                    who,
                    command=".观星",
                    lead_seconds=20,
                )

            today = gazing_date or datetime.now().strftime("%Y-%m-%d")
            if avatar:
                # 化身模式：检查该化身今天是否已观星
                if self.get_avatar_state(avatar).get("last_gazing_date") == today:
                    log.info(f"Star gazing [{avatar}]: already observed on {today}; skipping.")
                    return
                if manifest_dt and not self.star_gazing_claim_matches(avatar, manifest_dt):
                    claimed = self.state.get("star_gazing_claimed_avatar", "")
                    claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                    log.info(
                        f"Star gazing [{avatar}]: stale task skipped for {dt_to_str(manifest_dt)}; "
                        f"claimed by {claimed or 'none'} ({claimed_manifest or 'none'})."
                    )
                    return
            else:
                # 主魂模式：原有逻辑
                if self.star_gazing_sent_on_date(today):
                    log.info(f"Star gazing: .观星 already sent on {today}; skipping.")
                    self.clear_pending_star_gazing_schedule()
                    self.save_state()
                    return

            if datetime.now() < send_dt - timedelta(seconds=1):
                return  # 还没到时间，可能是被提前唤醒了

            # 发送 .观星
            who = avatar or "主魂"
            if self.dashboard_command_paused(".观星", who):
                log.info(f"Star gazing [{who}]: .观星 paused by dashboard; clearing pending schedule.")
                self.clear_pending_star_gazing_schedule()
                self.clear_star_gazing_round_claim()
                self.save_state()
                return
            self.active_atomic_task = asyncio.current_task()
            log.info(f"🔒 [ATOMIC LOCK] Acquired by StarGazing-{who}")
            try:
                if avatar:
                    log.info(f"Star gazing [{avatar}]: sending .观星 at {dt_to_str(send_dt)}.")
                    resp_msg = await self.send_and_wait_feedback_identity(
                        avatar, ".观星",
                        timeout=30,
                        max_retries=0,
                        return_response_msg=True,
                        delete_after=False,
                        force_identity_check=True,
                        suppress_no_response_alert=True,
                    )
                else:
                    log.info(f"Star gazing: sending .观星 at {dt_to_str(send_dt)}.")
                    resp_msg = await self.send_and_wait_feedback_identity(
                        "主魂",
                        ".观星",
                        timeout=30,
                        max_retries=0,
                        return_response_msg=True,
                        delete_after=False,
                        force_identity_check=True,
                        suppress_no_response_alert=True,
                    )

                if not resp_msg:
                    if avatar:
                        log.info(
                            f"Star gazing [{avatar}]: .观星 no direct response; "
                            "keeping today's chance available."
                        )
                    else:
                        log.info(
                            "Star gazing: .观星 no direct response; "
                            "will retry later or fallback at 23:59."
                        )
                        # 不要在这里设置 last_gazing_date，否则今天直接报废
                        self.clear_pending_star_gazing_schedule()
                        self.save_state()
                    return

                # 有回复才算作今日已观星
                if avatar:
                    self.set_avatar_state(avatar, "last_gazing_date", today)
                    self.set_avatar_state(avatar, "last_gazing_time", now_str())
                    # ⚠️ 同步更新主 state，防止 star_gazing_sent_on_date() 漏检导致重复派化身
                    self.state["last_gazing_date"] = today
                    self.state["last_gazing_time"] = now_str()
                    self.common_mark_star_gazing_round_assigned(
                        manifest_dt,
                        avatar,
                        source=".观星 response",
                        logger=log,
                    )
                else:
                    self.state["last_gazing_date"] = today
                    self.state["last_gazing_time"] = now_str()
                    self.common_mark_star_gazing_round_assigned(
                        manifest_dt,
                        "主魂",
                        source=".观星 response",
                        logger=log,
                    )
                    self.clear_pending_star_gazing_schedule()
                    self.save_state()

                # 如果结果是 Good，触发改换星移
                resp_text = (resp_msg.text or "")
                if self.star_gazing_good_opportunity(resp_text):
                    if immediate_shift:
                        # ---- 当前窗口活跃：计算发送 .改换星移 的准确时间 ----
                        command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
                        who = avatar or "主魂"
                        if self.dashboard_command_paused(command, who):
                            log.info(f"Star gazing [{who}]: {command} paused by dashboard; skipping active shift.")
                            if not avatar:
                                self.clear_pending_star_shift()
                            self.save_state()
                            return
                        current_manifest_dt = manifest_dt
                        if current_manifest_dt is None:
                            current_manifest_hour = (datetime.now().hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
                            current_manifest_dt = datetime.now().replace(
                                hour=current_manifest_hour, minute=0, second=0, microsecond=0
                            )
                        fate_type = self.star_gazing_manifest_fate_type(resp_text) or self.state.get("pending_star_gazing_fate_type", "")
                        shift_dt = star_gazing_shift_dt(current_manifest_dt, fate_type=fate_type, logger=log)
                    
                        now2 = datetime.now()
                        if now2 < shift_dt:
                            wait_sec = (shift_dt - now2).total_seconds()
                            who = avatar or "主魂"
                            log.info(
                                f"Star gazing [{who}]: GOOD result during ACTIVE window, "
                                f"but too early for shift. Waiting {wait_sec:.1f}s until {dt_to_str(shift_dt)}."
                            )
                            await asyncio.sleep(wait_sec)
                        elif now2 > shift_dt + timedelta(seconds=STAR_GAZING_SHIFT_GRACE_SECONDS):
                            who = avatar or "主魂"
                            log.info(
                                f"Star gazing [{who}]: skipped ACTIVE window shift; "
                                f"configured send window ended at {dt_to_str(shift_dt)}."
                            )
                            return
                        who = avatar or "主魂"
                        if avatar and not self.star_gazing_claim_matches(avatar, current_manifest_dt):
                            log.info(
                                f"Star gazing [{who}]: round claim cleared for "
                                f"{dt_to_str(current_manifest_dt)}; skipping .改换星移."
                            )
                            return
                        if self.star_gazing_final_report_seen(current_manifest_dt):
                            who = avatar or "主魂"
                            log.info(
                                f"Star gazing [{who}]: final report already seen for "
                                f"{dt_to_str(current_manifest_dt)}; skipping .改换星移."
                            )
                            return
                    
                        who = avatar or "主魂"
                        if not self.common_mark_star_shift_attempt(
                            who,
                            today,
                            source="active-window dispatch",
                            logger=log,
                        ):
                            return
                        self.save_state()
                        log.info(
                            f"Star gazing [{who}]: sending {command} in ACTIVE window as reply to msg {resp_msg.id}."
                        )
                        if avatar:
                            sent_msg = await self.send_and_wait_feedback_identity(
                                avatar,
                                command,
                                reply_to=resp_msg.id,
                                suppress_no_response_alert=True,
                            )
                        else:
                            sent_msg = await self.send_to_game(command, reply_to=resp_msg.id)
                        if sent_msg:
                            if avatar:
                                self.set_avatar_state(avatar, "last_star_shift_date", today)
                                self.set_avatar_state(avatar, "last_star_shift_time", now_str())
                            else:
                                self.state["last_star_shift_date"] = today
                                self.state["last_star_shift_time"] = now_str()
                            log.info(f"Star gazing [{who}]: .改换星移 sent successfully in immediate mode.")
                        else:
                            log.info(
                                f"Star gazing [{who}]: .改换星移 has no confirmed feedback in immediate mode; "
                                "possible control-field/no-response window."
                            )
                        if not avatar:
                            self.clear_pending_star_shift()
                            self.save_state()
                    else:
                        # ---- 常规模式：排期到下一个显化窗口 ----
                        target_dt = manifest_dt or self.next_star_manifest_dt(datetime.now())
                        target_day = target_dt.strftime("%Y-%m-%d")
                        if self.star_gazing_final_report_seen(target_dt):
                            who = avatar or "主魂"
                            log.info(
                                f"Star gazing [{who}]: final report already seen for {dt_to_str(target_dt)}; "
                                "not scheduling .改换星移."
                            )
                            return
                        if avatar:
                            if self.get_avatar_state(avatar).get("last_star_shift_date") != target_day:
                                log.info(
                                    f"Star gazing [{avatar}]: GOOD result; scheduling .改换星移 for manifest {dt_to_str(target_dt)}."
                                )
                                asyncio.create_task(
                                    self.avatar_schedule_star_shift(avatar, resp_msg.id, target_dt, target_day)
                                )
                        else:
                            if not self.star_shift_done_today(target_day):
                                self.state["pending_star_shift_date"] = target_day
                                self.state["pending_star_shift_target_time"] = dt_to_str(target_dt)
                                self.state["pending_star_shift_msg_id"] = resp_msg.id
                                self.save_state()
                                log.info(
                                    f"Star gazing: GOOD result; scheduling .改换星移 for manifest {dt_to_str(target_dt)}."
                                )
                                self.star_shift_task = asyncio.create_task(
                                    self.schedule_star_shift(resp_msg.id, target_dt, target_day)
                                )
                else:
                    who = avatar or "主魂"
                    log.info(
                        f"Star gazing [{who}]: .观星 result does not contain GOOD keyword; skipping .改换星移."
                    )
            finally:
                if self.active_atomic_task == asyncio.current_task():
                    self.active_atomic_task = None
                    log.info(f"🔓 [ATOMIC LOCK] Released by StarGazing-{who}")

    async def maybe_handle_star_gazing_opportunity(self, msg, text, sender):
            """
            当收到一条新消息时，检查是否为 Good 级别的星盘显化事件，进行以下处理：

            1. 如果文本包含我们关注的 Good 关键词（STAR_GAZING_GOOD_KEYWORDS），
               调度 .观星 在下一个显化窗口前 1 分钟发送。
            2. 如果文本包含其他 Good 关键词（不在我们的目标列表中），
               且当前有排期中的 .观星，则取消排期以保留每日观星机会给 23:59 兜底。
            3. 跨日显化按目标显化日/实际发送日判断，避免 0 点机会被前一天记录挡掉。

            返回:
                True 表示消息已被处理（是 Good 显化类事件），False 表示不是。
            """
            # 检查是否为我们关注的 Good 关键词（用于改换星移）
            is_our_good = self.star_gazing_good_opportunity(text)
            # 检查是否为任意 Good 级别事件（包括不在目标列表中的）
            is_any_good = bool(text and "【Good -" in text)
            fate_type = self.star_gazing_manifest_fate_type(text)
            is_manifest_notice = bool(text and "【星盘显化】" in text and fate_type)

            if not is_any_good and not is_manifest_notice:
                return False
            if not sender or not is_game_bot_sender(self, sender):
                return False

            now = datetime.now()
            notice_manifest_dt = self.star_gazing_manifest_for_notice(now)
            manifest_dt = notice_manifest_dt or self.star_gazing_target_for_opportunity(now)
            manifest_key = dt_to_str(manifest_dt)
            # 构造发送者信息用于日志
            sender_info = (
                f"@{sender.username}"
                if sender and sender.username
                else f"id={msg.sender_id}"
            )
            text_preview = (text[:150] + "...") if len(text) > 150 else text

            if is_manifest_notice and not is_any_good:
                async with self.star_gazing_lock:
                    pending = self.state.get("pending_star_gazing_target_time", "")
                    pending_manifest = (
                        self.state.get("pending_star_gazing_manifest_time", "")
                        or self.state.get("star_gazing_claimed_manifest_time", "")
                    )
                    pending_manifest_dt = str_to_dt(pending_manifest)
                    current_manifest_dt = self.current_star_report_manifest_dt(now)
                    cancels_pending_manifest = (
                        pending_manifest == manifest_key
                        or bool(pending_manifest_dt and pending_manifest_dt <= current_manifest_dt)
                    )
                    if pending_manifest and cancels_pending_manifest:
                        self.clear_pending_star_gazing_schedule()
                        self.clear_star_gazing_round_claim()
                        if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
                            self.star_gazing_task.cancel()
                        self.state["next_star_gazing_time"] = ""
                        self.save_state()
                        log.info(
                            f"Star gazing: CANCELLED pending .观星 (was at {pending}) "
                            f"for manifest {manifest_key}; updated fate is {fate_type}. "
                            f"Detected by {sender_info}: {text_preview}"
                        )
                return True

            if is_our_good:
                if notice_manifest_dt is None:
                    log.info(
                        f"Star gazing: target Good notice arrived after this round's final report; "
                        f"ignoring without consuming .观星. Triggered by {sender_info}: {text_preview}"
                    )
                    return True

                send_dt, immediate_shift, gazing_date = self.star_gazing_schedule_plan(now, manifest_dt)
                immediate_shift = True

                async with self.star_gazing_lock:
                    self.clear_stale_star_gazing_claim_before_manifest(
                        manifest_dt,
                        sender_info=sender_info,
                        text_preview=text_preview,
                    )
                    claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                    claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                    if claimed_manifest and claimed_avatar and self.claimed_star_gazing_pending_due(claimed_avatar, now):
                        claimed_manifest_dt = str_to_dt(claimed_manifest)
                        if claimed_manifest_dt:
                            manifest_dt = claimed_manifest_dt
                            manifest_key = claimed_manifest
                            gazing_date = self.state.get("pending_star_gazing_date", "") or gazing_date
                    if claimed_manifest == manifest_key and claimed_avatar:
                        if self.maybe_record_passive_claimed_star_gazing_result(
                            claimed_avatar,
                            manifest_dt,
                            gazing_date,
                            msg,
                        ):
                            return True
                        log.info(
                            f"Star gazing: manifest {manifest_key} already assigned to {claimed_avatar}; "
                            f"skip duplicate trigger from {sender_info}: {text_preview}"
                        )
                        return True

                    assigned_avatar = self.common_star_gazing_assigned_avatar_for_manifest(manifest_dt)
                    if assigned_avatar:
                        log.info(
                            f"Star gazing: manifest {manifest_key} already spent by {assigned_avatar}; "
                            f"skip duplicate trigger from {sender_info}: {text_preview}"
                        )
                        return True

                    selected_avatar, idx = self.choose_star_gazing_avatar_for_today(gazing_date)
                    if not selected_avatar:
                        log.info(f"观星轮换: {gazing_date} 所有化身都已观星，跳过本轮显化。")
                        return True

                    # 清除旧的排期，并安全取消已有后台任务，防止并发多次发送
                    self.clear_pending_star_gazing_schedule()
                    if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
                        self.star_gazing_task.cancel()
                        log.info("Star gazing: cancelled previous pending task to avoid concurrent runs.")

                    self.state["pending_star_gazing_date"] = gazing_date
                    self.state["pending_star_gazing_target_time"] = dt_to_str(send_dt)
                    self.state["pending_star_gazing_scheduled_time"] = dt_to_str(send_dt)
                    self.state["pending_star_gazing_manifest_time"] = manifest_key
                    self.state["pending_star_gazing_fate_type"] = self.star_gazing_pending_fate_type(text)
                    self.state["star_gazing_claimed_manifest_time"] = manifest_key
                    self.state["star_gazing_claimed_avatar"] = selected_avatar
                    self.common_mark_star_gazing_round_assigned(
                        manifest_dt,
                        selected_avatar,
                        source="manifest opportunity",
                        logger=log,
                    )
                    self.state["next_star_gazing_time"] = dt_to_str(send_dt)
                    self.save_state()

                avatars = STAR_GAZING_ROTATING_AVATARS
                log.info(
                    f"观星轮换: 显化 {dt_to_str(manifest_dt)} 指派 {selected_avatar} "
                    f"(索引 {idx}), 下次轮换至 {avatars[self.state.get('star_gazing_avatar_index', 0) % len(avatars)]}"
                )
                if immediate_shift:
                    log.info(
                        f"Star manifest GOOD detected DURING active window "
                        f"({dt_to_str(manifest_dt)}); dispatching {selected_avatar} to send .观星 IMMEDIATELY. "
                        f"Triggered by {sender_info}: {text_preview}"
                    )
                else:
                    log.info(
                        f"Star manifest GOOD detected at {dt_to_str(now)}; "
                        f"dispatching {selected_avatar} to send .观星 at {dt_to_str(send_dt)}. "
                        f"Triggered by {sender_info}: {text_preview}"
                    )

                # 创建后台任务执行观星（使用轮换化身）
                self.star_gazing_task = asyncio.create_task(
                    self.schedule_star_gazing_simple(
                        send_dt,
                        immediate_shift=immediate_shift,
                        avatar=selected_avatar,
                        manifest_dt=manifest_dt,
                        gazing_date=gazing_date,
                    )
                )
                return True
            else:
                # ---- 非目标 Good 关键词：取消排期中的 .观星，保留每日机会 ----
                async with self.star_gazing_lock:
                    pending = self.state.get("pending_star_gazing_target_time", "")
                    if pending and is_future(pending):
                        # 有排期中的 .观星 → 取消（下一个显化事件已不是我们的目标）
                        self.clear_pending_star_gazing_schedule()
                        self.clear_star_gazing_round_claim()
                        if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
                            self.star_gazing_task.cancel()
                        self.state["next_star_gazing_time"] = ""
                        self.save_state()
                        log.info(
                            f"Star gazing: CANCELLED pending .观星 (was at {pending}). "
                            f"Upcoming event changed to non-target keyword. "
                            f"Detected by {sender_info}: {text_preview}"
                        )
                return True

    async def run_star_gazing_loop(self):
            """
            全天候观星监听循环。
            功能：
              1. 启动时恢复待执行的改换星移/观星排期。
              2. 每 10 分钟检查一次：
                 a. 是否需要执行每日备用观星（23:59 兜底）。
                 b. 更新 next_star_gazing_time 和 next_star_manifest_time 的显示。
              3. 等待期间不阻塞显化事件监听（由 handle_game_response 处理）。
            """
            await self.startup_done.wait()

            # ---- 启动恢复：检查待执行的改换星移 ----
            pending_date = self.state.get("pending_star_shift_date", "")
            pending_target = self.state.get("pending_star_shift_target_time", "")
            pending_msg_id = int(self.state.get("pending_star_shift_msg_id") or 0)
            if (
                pending_date and pending_target and pending_msg_id
                and not self.star_shift_done_today(pending_date)
            ):
                target_dt = str_to_dt(pending_target)
                if datetime.now() < target_dt:
                    log.info(
                        f"Star gazing: restoring pending shift for {pending_target}, "
                        f"reply msg {pending_msg_id}."
                    )
                    self.star_shift_task = asyncio.create_task(
                        self.schedule_star_shift(pending_msg_id, target_dt, pending_date)
                    )
                else:
                    log.info(
                        f"Star gazing: clearing expired pending shift for {pending_target}."
                    )
                    self.clear_pending_star_shift()
                    self.save_state()

            pending_gazing_date = self.state.get("pending_star_gazing_date", "")
            pending_gazing_send = (
                self.state.get("pending_star_gazing_scheduled_time", "")
                or self.state.get("pending_star_gazing_target_time", "")
            )
            pending_manifest = (
                self.state.get("pending_star_gazing_manifest_time", "")
                or self.state.get("star_gazing_claimed_manifest_time", "")
            )
            pending_avatar = self.state.get("star_gazing_claimed_avatar", "")
            pending_fate_type = self.state.get("pending_star_gazing_fate_type", "")
            if pending_gazing_send and pending_manifest and pending_avatar and "Good" not in pending_fate_type:
                log.info(
                    f"Star gazing: clearing pending .观星 for {pending_manifest}; "
                    "missing confirmed Good fate marker."
                )
                self.clear_pending_star_gazing_schedule()
                self.clear_star_gazing_round_claim()
                self.save_state()
                pending_gazing_send = ""
            if pending_gazing_send and pending_manifest and pending_avatar:
                send_dt = str_to_dt(pending_gazing_send)
                manifest_dt = str_to_dt(pending_manifest)
                latest_send_dt = manifest_dt - timedelta(seconds=60) if manifest_dt else None
                pending_send_is_valid = bool(
                    send_dt
                    and is_future(pending_gazing_send)
                    and (not latest_send_dt or send_dt <= latest_send_dt)
                )
                if not pending_send_is_valid:
                    expired_send = pending_gazing_send
                    pending_gazing_send = ""
                    log.info(
                        f"Star gazing: clearing expired pending .观星 for {pending_avatar}; "
                        f"send={expired_send}, manifest={pending_manifest}."
                    )
                    self.clear_pending_star_gazing_schedule()
                    self.clear_star_gazing_round_claim()
                    self.save_state()
            if pending_gazing_send and pending_manifest and pending_avatar:
                pending_gazing_date = pending_gazing_date or send_dt.strftime("%Y-%m-%d")
                preferred_send_dt = manifest_dt - timedelta(
                    seconds=max(60, int(STAR_GAZING_COMMAND_LEAD_SECONDS))
                ) if manifest_dt else None
                if preferred_send_dt and preferred_send_dt > datetime.now() and send_dt != preferred_send_dt:
                    send_dt = preferred_send_dt
                    pending_gazing_send = dt_to_str(send_dt)
                    self.state["pending_star_gazing_target_time"] = pending_gazing_send
                    self.state["pending_star_gazing_scheduled_time"] = pending_gazing_send
                    self.state["next_star_gazing_time"] = pending_gazing_send
                    self.save_state()
                if self.get_avatar_state(pending_avatar).get("last_gazing_date") != pending_gazing_date:
                    restore_immediate_shift = False
                    log.info(
                        f"Star gazing: restoring pending .观星 for {pending_avatar} "
                        f"at {pending_gazing_send}, manifest {pending_manifest}."
                    )
                    self.star_gazing_task = asyncio.create_task(
                        self.schedule_star_gazing_simple(
                            send_dt,
                            immediate_shift=restore_immediate_shift,
                            avatar=pending_avatar,
                            manifest_dt=manifest_dt,
                            gazing_date=pending_gazing_date,
                        )
                    )
                else:
                    log.info(
                        f"Star gazing: clearing pending .观星 for {pending_avatar}; "
                        f"already observed on {pending_gazing_date}."
                    )
                    self.clear_pending_star_gazing_schedule()
                    self.clear_star_gazing_round_claim()
                    self.save_state()



            # ---- 主循环 ----
            while self.is_running:
                await self._wait_for_main_identity()
                now = datetime.now()

                # 检查每日备用观星
                if await self.maybe_run_daily_star_gazing_fallback(now):
                    await asyncio.sleep(5)
                    continue

                # 更新状态中的 next_star_gazing_time 和 next_star_manifest_time
                # 这些仅用于日志和外部监控显示
                target_dt = self.next_star_manifest_dt(now)
                pending_gazing_target = self.state.get("pending_star_gazing_target_time", "")
                pending_gazing_scheduled = self.state.get("pending_star_gazing_scheduled_time", "")
                fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)

                if pending_gazing_target and pending_gazing_scheduled:
                    self.state["next_star_gazing_time"] = pending_gazing_scheduled
                elif fallback_dt:
                    self.state["next_star_gazing_time"] = dt_to_str(fallback_dt)
                else:
                    self.state["next_star_gazing_time"] = ""
                self.state["next_star_manifest_time"] = dt_to_str(target_dt)
                self.save_state()

                # 如果备用观星时间还没到，等待
                if fallback_dt and now < fallback_dt:
                    wait_sec = (fallback_dt - now).total_seconds()
                    log.info(
                        f"Star gazing fallback waiting {int(wait_sec)}s until "
                        f"{self.state['next_star_gazing_time']}. "
                        f"Good opportunity listener remains active for manifest "
                        f"{self.state['next_star_manifest_time']}."
                    )
                    await asyncio.sleep(scheduler_sleep_seconds(wait_sec))
                    continue

                log.info(
                    f"Star gazing listener active all day for "
                    f"{', '.join(STAR_GAZING_GOOD_KEYWORDS)}. "
                    f"Next manifest {self.state['next_star_manifest_time']}."
                )
                await asyncio.sleep(scheduler_sleep_seconds(600))

    # ---- 化身星辰牵引 / 安抚 / 收集（素缘子，观星抢占逻辑保持独立不变）----

    def response_text(self, resp):
            return self.common_response_text(resp)

    def recent_command_guard_wait(self, command="", max_age_seconds=15):
            return self.common_recent_command_guard_wait(command, max_age_seconds=max_age_seconds)

    def apply_avatar_star_guard_backoff(self, avatar, command="", fields=None, reason="command guard"):
            return self.common_apply_avatar_star_guard_backoff(
                avatar,
                command=command,
                fields=fields,
                reason=reason,
                logger=log,
            )

    def compact_seconds(self, seconds):
            return self.common_compact_seconds(seconds)

    def avatar_star_due(self, avatar, key):
            return self.common_avatar_star_due(avatar, key)

    def avatar_star_recently_appeased(self, avatar, window_seconds=180):
            return self.common_avatar_star_recently_appeased(
                avatar,
                window_seconds=window_seconds,
                now=datetime.now(),
            )

    def parse_avatar_star_observatory(self, text):
            return self.common_parse_avatar_star_observatory(text)

    def record_avatar_star_observatory(self, avatar, text, source="观星台"):
            return self.common_record_avatar_star_observatory(
                avatar,
                text,
                source=source,
                star_target=STAR_ATTRACTION_TARGET,
                pre_appease_lead_seconds=STAR_PRE_APPEASE_LEAD_SECONDS,
                status_retry_seconds=STAR_STATUS_RETRY_SECONDS,
                logger=log,
            )

    def _star_cycle_collect_time_from_now(self):
            return self.common_star_cycle_collect_time_from_now(STAR_ATTRACTION_COOLDOWN_SECONDS)

    def record_avatar_star_pull_response(self, avatar, text, source=STAR_ATTRACTION_COMMAND):
            return self.common_record_avatar_star_pull_response(
                avatar,
                text,
                source=source,
                star_command=STAR_ATTRACTION_COMMAND,
                star_target=STAR_ATTRACTION_TARGET,
                pre_appease_lead_seconds=STAR_PRE_APPEASE_LEAD_SECONDS,
                status_retry_seconds=STAR_STATUS_RETRY_SECONDS,
                cooldown_seconds=STAR_ATTRACTION_COOLDOWN_SECONDS,
                logger=log,
            )

    def record_avatar_star_appease_response(self, avatar, text, source=".安抚星辰"):
            return self.common_record_avatar_star_appease_response(
                avatar,
                text,
                source=source,
                status_retry_seconds=STAR_STATUS_RETRY_SECONDS,
                logger=log,
            )

    def record_avatar_star_collect_response(self, avatar, text, source=".收集精华"):
            return self.common_record_avatar_star_collect_response(
                avatar,
                text,
                source=source,
                pre_appease_lead_seconds=STAR_PRE_APPEASE_LEAD_SECONDS,
                logger=log,
            )

    def record_avatar_star_response_from_text(self, avatar, text, source="star sync"):
            return self.common_record_avatar_star_response_from_text(
                avatar,
                text,
                source=source,
                star_avatars=STAR_ATTRACTION_AVATARS,
                star_target=STAR_ATTRACTION_TARGET,
            )

    async def refresh_avatar_star_observatory(self, avatar, reason="init"):
            log.info(f"Avatar [{avatar}] star observatory refresh: {reason}")
            resp = await self.send_and_wait_feedback_identity(avatar, ".观星台", timeout=45, max_retries=1)
            text = self.response_text(resp)
            if not text and self.apply_avatar_star_guard_backoff(
                avatar, "", fields=["next_star_check_time"], reason=f".观星台/{reason}"
            ):
                return False
            if self.record_avatar_star_observatory(avatar, text, source=f".观星台/{reason}"):
                return True
            retry_at = add_seconds_str(now_str(), STAR_STATUS_RETRY_SECONDS)
            self.update_avatar_states(avatar, {
                "star_observatory_needs_refresh": True,
                "next_star_check_time": retry_at,
            })
            if text:
                notify_unrecognized_response(self, ".观星台", text, log, f"观星台/{avatar}/{reason}")
            return False

    async def restart_avatar_deep_meditation_after_star(self, avatar, reason):
            try:
                log.info(f"Avatar [{avatar}] restarting deep meditation after star action: {reason}")
                result = await self.run_avatar_meditation_restart_chain(
                    avatar,
                    source=f"star restart {reason}",
                    check_timeout=30,
                    check_retries=1,
                    cultivation_timeout=45,
                    cultivation_retries=1,
                    deep_timeout=60,
                    deep_retries=1,
                )
                if result.get("status") == "unknown_check":
                    notify_unrecognized_response(
                        self, ".查看闭关", result.get("text", ""), log, f"观星后闭关/{avatar}/{reason}"
                    )
            except Exception as e:
                log.error(f"Avatar [{avatar}] failed to restart deep meditation after star action: {e}")

    async def attempt_avatar_star_pull(self, avatar):
            resp = await self.send_and_wait_feedback_identity(avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1)
            text = self.response_text(resp)
            if not text and self.apply_avatar_star_guard_backoff(
                avatar, "", fields=["star_attraction_retry_time", "next_star_attraction_time"], reason=STAR_ATTRACTION_COMMAND
            ):
                return "guarded"
            result = self.record_avatar_star_pull_response(avatar, text)

            if result == "no_free":
                await self.refresh_avatar_star_observatory(avatar, reason="no free disk after pull")
            elif result in {"empty", "unknown"}:
                await self.refresh_avatar_star_observatory(avatar, reason=f"pull {result}")

            if result != "insufficient":
                return result

            state = self.get_avatar_state(avatar)
            if state.get("star_attraction_force_exit_tried"):
                retry_at = add_seconds_str(now_str(), STAR_INSUFFICIENT_RETRY_SECONDS)
                self.update_avatar_states(avatar, {
                    "star_attraction_retry_time": retry_at,
                    "next_star_attraction_time": retry_at,
                    "next_star_check_time": retry_at,
                })
                log.warning(f"Avatar [{avatar}] star pull still 修为不足; retry at {retry_at}.")
                await self.restart_avatar_deep_meditation_after_star(avatar, "star pull still insufficient")
                return result

            log.warning(f"Avatar [{avatar}] star pull 修为不足; forcing exit and retrying once.")
            self.update_avatar_states(avatar, {"star_attraction_force_exit_tried": True})
            await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=45, max_retries=1)
            self.update_avatar_states(avatar, {
                "in_deep_meditation": False,
                "deep_meditation_end_time": "",
            })
            await asyncio.sleep(3)

            retry_resp = await self.send_and_wait_feedback_identity(avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1)
            retry_text = self.response_text(retry_resp)
            retry_result = self.record_avatar_star_pull_response(avatar, retry_text, source=f"{STAR_ATTRACTION_COMMAND} retry")
            if retry_result == "insufficient":
                retry_at = add_seconds_str(now_str(), STAR_INSUFFICIENT_RETRY_SECONDS)
                self.update_avatar_states(avatar, {
                    "star_attraction_retry_time": retry_at,
                    "next_star_attraction_time": retry_at,
                    "next_star_check_time": retry_at,
                })
                log.warning(f"Avatar [{avatar}] star pull still 修为不足 after force exit; retry at {retry_at}.")
            elif retry_result == "no_free":
                await self.refresh_avatar_star_observatory(avatar, reason="no free disk after forced retry")
            elif retry_result in {"empty", "unknown"}:
                await self.refresh_avatar_star_observatory(avatar, reason=f"forced retry {retry_result}")

            await self.restart_avatar_deep_meditation_after_star(avatar, "star pull force-exit chain")
            return retry_result

    async def attempt_avatar_star_appease(self, avatar):
            resp = await self.send_and_wait_feedback_identity(avatar, ".安抚星辰", timeout=45, max_retries=1)
            text = self.response_text(resp)
            if not text and self.apply_avatar_star_guard_backoff(
                avatar, "", fields=["next_star_appease_time"], reason=".安抚星辰"
            ):
                return "guarded"
            result = self.record_avatar_star_appease_response(avatar, text)
            if result == "unknown":
                await self.refresh_avatar_star_observatory(avatar, reason="appease abnormal")
            return result

    async def attempt_avatar_star_collect(self, avatar):
            resp = await self.send_and_wait_feedback_identity(avatar, ".收集精华", timeout=45, max_retries=1)
            text = self.response_text(resp)
            if not text and self.apply_avatar_star_guard_backoff(
                avatar, "", fields=["next_star_collect_time"], reason=".收集精华"
            ):
                return "guarded"
            result = self.record_avatar_star_collect_response(avatar, text)
            if result in {"not_ready", "unknown"}:
                await self.refresh_avatar_star_observatory(avatar, reason=f"collect {result}")
            elif result == "success":
                await asyncio.sleep(3)
                await self.attempt_avatar_star_pull(avatar)
            return result

    async def maybe_run_avatar_star_cycle(self, avatar):
            if avatar not in STAR_ATTRACTION_AVATARS:
                return

            async with AtomicTaskContext(self, f"AvatarStarAttraction-{avatar}"):
                for _ in range(5):
                    state = self.get_avatar_state(avatar)
                    retry_time = state.get("star_attraction_retry_time", "")
                    if retry_time and is_future(retry_time):
                        return

                    if self.avatar_star_due(avatar, "next_star_appease_time"):
                        result = await self.attempt_avatar_star_appease(avatar)
                        if result == "success":
                            state = self.get_avatar_state(avatar)
                            collect_key = state.get("next_star_collect_time", "")
                            if collect_key and not is_future(collect_key):
                                self.update_avatar_states(avatar, {"star_pre_collect_appeased_for": collect_key})
                        continue

                    if self.avatar_star_due(avatar, "next_star_collect_time"):
                        state = self.get_avatar_state(avatar)
                        collect_key = state.get("next_star_collect_time", "")
                        appease_time = state.get("next_star_appease_time", "")
                        if appease_time and is_future(appease_time):
                            return
                        if state.get("star_pre_collect_appeased_for") != collect_key:
                            if self.avatar_star_recently_appeased(avatar):
                                self.update_avatar_states(avatar, {"star_pre_collect_appeased_for": collect_key})
                            else:
                                result = await self.attempt_avatar_star_appease(avatar)
                                if result == "success":
                                    self.update_avatar_states(avatar, {"star_pre_collect_appeased_for": collect_key})
                                    continue
                                return
                        await self.attempt_avatar_star_collect(avatar)
                        continue

                    if self.avatar_star_due(avatar, "next_star_attraction_time") and not is_future(state.get("next_star_collect_time", "")):
                        await self.attempt_avatar_star_pull(avatar)
                        continue

                    check_time = state.get("next_star_check_time", "")
                    check_due = not check_time or not is_future(check_time)
                    needs_init = (
                        not state.get("last_star_observatory_time")
                        and not state.get("next_star_collect_time")
                        and not is_future(state.get("next_star_attraction_time", ""))
                    )
                    if (
                        (state.get("star_observatory_needs_refresh") and check_due)
                        or (needs_init and check_due)
                        or self.avatar_star_due(avatar, "next_star_check_time")
                    ):
                        ok = await self.refresh_avatar_star_observatory(avatar, reason="scheduled/init")
                        if not ok:
                            return
                        continue

                    if not any(state.get(k) for k in [
                        "next_star_check_time",
                        "next_star_appease_time",
                        "next_star_collect_time",
                        "next_star_attraction_time",
                    ]):
                        ok = await self.refresh_avatar_star_observatory(avatar, reason="missing schedule")
                        if not ok:
                            return
                        continue

                    return

    def next_avatar_star_wait_seconds(self, avatar):
            return self.common_next_avatar_star_wait_seconds(avatar)

    async def run_avatar_star_attraction_loop(self, avatar, initial_delay=0):
            """星宫化身观星台牵引循环：牵引 -> 安抚 -> 收集 -> 再牵引。"""
            await self.startup_done.wait()
            self._avatar_loop_count += 1
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)

            while self.is_running:
                try:
                    await self.maybe_run_avatar_star_cycle(avatar)
                    wait_sec = self.next_avatar_star_wait_seconds(avatar)
                    if wait_sec <= 0:
                        sleep_for = 30
                    elif wait_sec <= 180:
                        sleep_for = max(5, wait_sec)
                    else:
                        sleep_for = min(wait_sec, 1800)
                    log.info(f"Avatar [{avatar}] star attraction loop sleeping {int(sleep_for)}s.")
                    await asyncio.sleep(sleep_for)
                except Exception as e:
                    log.error(f"Avatar [{avatar}] star attraction loop error: {e}", exc_info=True)
                    self.update_avatar_states(avatar, {
                        "star_observatory_needs_refresh": True,
                        "next_star_check_time": add_seconds_str(now_str(), STAR_STATUS_RETRY_SECONDS),
                    })
                    await asyncio.sleep(60)

    def formation_invite_actor_username(self, text):
            """提取阵法邀请里的发起者 @username。"""
            return CommonCommandMixin.formation_invite_actor_username(self, text)

    def avatar_username_for_identity(self, avatar):
            return CommonCommandMixin.avatar_username_for_identity(self, avatar)

    def formation_result_includes_avatar(self, text, avatar):
            return CommonCommandMixin.formation_result_includes_avatar(self, text, avatar)

    def is_target_formation_invite(self, text):
            """只监听副号三个分身发起的星宫启阵邀请。"""
            if not text or self.is_formation_success(text):
                return False
            if not (
                "周天星斗大阵-启" in text
                and "正在布设大阵" in text
                and ("尚需" in text or "助阵" in text)
            ):
                return False
            return self.formation_invite_actor_username(text) in FORMATION_TARGET_INITIATORS

    async def prepare_avatar_for_formation_assist(self, avatar):
            """助阵前检查化身是否可直接助阵；强行出关只在助阵成功 5小时55分后执行。"""
            a_state = self.get_avatar_state(avatar)
            if a_state.get("in_deep_meditation"):
                log.info(f"Avatar [{avatar}] formation assist: trying direct assist while in deep meditation; no force exit before success.")
            return True

    async def maybe_assist_target_formation_invite(self, formation_msg):
            """实时响应副号三分身的阵法邀请。"""
            if not formation_msg:
                return False
            text = formation_msg.text or ""
            if not self.is_target_formation_invite(text):
                return False
            age = self.message_age_seconds(formation_msg)
            if age > 60:
                log.info(f"Target formation invite {formation_msg.id} ignored: stale ({int(age)}s).")
                return False
            if self.formation_assist_in_progress:
                return False

            initiator = self.formation_invite_actor_username(text)
            initiator_label = FORMATION_TARGET_INITIATORS.get(initiator, initiator)
            self.formation_assist_in_progress = True
            self.pending_formation_invite_msg = formation_msg
            try:
                log.info(
                    f"Target formation invite detected from {initiator_label} "
                    f"(@{initiator}), msg={formation_msg.id}."
                )
                for avatar in FORMATION_ASSIST_AVATARS:
                    if not self.avatar_features.get(avatar, {}).get("formation_assist"):
                        continue
                    if self.dashboard_command_paused(".助阵", avatar):
                        log.info(f"Avatar [{avatar}] assist skipped: .助阵 paused by dashboard.")
                        continue
                    a_state = self.get_avatar_state(avatar)
                    next_form = a_state.get("next_formation_time", "")
                    if next_form and is_future(next_form):
                        log.info(f"Avatar [{avatar}] assist skipped: formation CD until {next_form}.")
                        continue
                    if self.message_age_seconds(formation_msg) > 55:
                        log.info(f"Avatar [{avatar}] assist skipped: invite nearly expired.")
                        break
                    if not await self.prepare_avatar_for_formation_assist(avatar):
                        continue
                    if self.message_age_seconds(formation_msg) > 60:
                        log.info(f"Avatar [{avatar}] assist skipped: invite expired after preparation.")
                        break
                    assisted = await self._avatar_assist_formation(avatar)
                    if assisted:
                        return True
                return False
            finally:
                if self.pending_formation_invite_msg is formation_msg:
                    self.pending_formation_invite_msg = None
                self.formation_assist_in_progress = False

    def is_formation_success(self, text):
            """检测阵法是否已成（周天星斗大阵-成 或 大阵已成）。"""
            return CommonCommandMixin.is_formation_success(self, text)

    def is_formation_pending(self, text):
            """检测阵法是否正在召集助阵（周天星斗大阵-启 或 尚需 或 助阵）。"""
            return CommonCommandMixin.is_formation_pending(self, text)

    def is_raw_formation_command(self, text):
            """检测是否用户直接输入了 .启阵 指令（不是机器人回复）。"""
            return (text or "").strip() == ".启阵"

    def is_formation_cooldown_active(self):
            """检测阵法冷却是否仍然有效（距上次启阵不足 12 小时）。"""
            last_formation = self.state.get("last_formation_time", "")
            return bool(
                last_formation and is_future(add_seconds_str(last_formation, 12 * 3600))
            )

    def is_formation_assist_success(self, text):
            if not text:
                return False
            return any(k in text for k in [
                "助阵成功", "成功助阵", "参与布阵", "加入大阵", "大阵已成",
                "周天星斗大阵-成", "阵成", "成功加入", "已参与", "已经参与",
                "已助阵", "已经助阵", "助阵完成", "已在阵中",
            ])

    def is_formation_assist_failure(self, text):
            if not text:
                return False
            return any(k in text for k in [
                "没有找到正在召集", "阵法已过期", "已过期", "过期",
                "没有找到", "无法助阵", "不能助阵", "助阵失败",
            ])

    async def _avatar_assist_formation(self, avatar):
            """
            化身助阵：使用 pending_formation_invite_msg 回复 .助阵。

            返回：
                True  — 助阵成功
                False — 助阵失败或无法助阵
            """
            invite_msg = self.pending_formation_invite_msg
            if not invite_msg or not hasattr(invite_msg, "id"):
                return False
            if not self.is_target_formation_invite(invite_msg.text or ""):
                self.pending_formation_invite_msg = None
                return False

            # 检查邀请消息是否还在有效期内（60秒）
            age = self.message_age_seconds(invite_msg)
            if age > 60:
                log.info(f"Avatar [{avatar}] assist: invite too old ({int(age)}s), clearing.")
                self.pending_formation_invite_msg = None
                return False

            log.info(f"Avatar [{avatar}] assisting formation via reply to invite {invite_msg.id}...")
            assist_resp = await self.send_and_wait_feedback_identity(
                avatar, ".助阵", reply_to=invite_msg.id,
                timeout=30, max_retries=1,
                suppress_no_response_alert=True,
            )
            assist_text = (getattr(assist_resp, "text", "") if assist_resp else "") if assist_resp else ""

            # 助阵成功
            if self.is_formation_success(assist_text) or self.is_formation_assist_success(assist_text):
                log.info(f"Avatar [{avatar}] formation assist SUCCESS!")
                self.pending_formation_invite_msg = None
                self.record_avatar_formation_success(avatar, self.message_effective_time_str(assist_resp))
                return True

            # 冷却中
            cd = self.parse_wait_time(assist_text)
            if cd > 0 and any(k in assist_text for k in ["冷却", "参与过布阵", "心神消耗", "再次启阵"]):
                log.info(f"Avatar [{avatar}] assist on cooldown ({cd}s).")
                self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd))
                self.set_avatar_state(avatar, "next_formation_retry_time", "")
                self.pending_formation_invite_msg = None
                return False

            # 助阵失败
            if self.is_formation_assist_failure(assist_text) or any(k in assist_text for k in ["已助阵"]):
                log.info(f"Avatar [{avatar}] assist failed: {assist_text[:80]}")
                return False

            for _ in range(15):
                await asyncio.sleep(1)
                try:
                    updated_msg = await self.client.get_messages(self.target_chat_id, ids=invite_msg.id)
                except Exception as e:
                    log.info(f"Avatar [{avatar}] assist poll failed: {e}")
                    break
                updated_text = (updated_msg.text or "") if updated_msg else ""
                if self.is_formation_success(updated_text):
                    if self.formation_result_includes_avatar(updated_text, avatar):
                        log.info(f"Avatar [{avatar}] formation assist confirmed by edited invite.")
                        self.pending_formation_invite_msg = None
                        self.record_avatar_formation_success(avatar, self.message_effective_time_str(updated_msg))
                        return True
                    log.info(f"Avatar [{avatar}] edited formation succeeded without this avatar; not recording CD.")
                    self.pending_formation_invite_msg = None
                    return False

            log.info(f"Avatar [{avatar}] assist response: {assist_text[:80]!r}")
            return False

    async def delayed_avatar_force_exit(self, avatar, delay_sec):
            """助阵成功 5小时55分后强行出关，并重新开启深度闭关。"""
            try:
                log.info(f"Avatar [{avatar}] force exit timer set for {int(delay_sec)}s.")
                if delay_sec > 0:
                    await asyncio.sleep(delay_sec)
                if not self.is_running:
                    return
                async with AtomicTaskContext(self, f"AvatarForceExit-{avatar}"):
                    await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=60)
                    self.update_avatar_states(avatar, {
                        "in_deep_meditation": False,
                        "deep_meditation_end_time": "",
                        "next_force_exit_time": "",
                        "meditation_restart_pending": True,
                        "meditation_restart_mode": "deep_only",
                    })
                    await asyncio.sleep(10)
                    await self.restart_avatar_deep_meditation_direct(avatar, "delayed force exit")
            except Exception as e:
                log.error(f"Avatar [{avatar}] delayed force exit error: {e}", exc_info=True)
                self.set_avatar_state(avatar, "next_force_exit_time", "")

    def record_avatar_formation_success(self, avatar, formation_time=None):
            """
            记录化身阵法成功后的状态更新（迁移自主循环 record_formation_success）。

            逻辑：
              - 设置 last_formation_time 为当前时间。
              - 设置 formation_active_until = 当前时间 + 6 小时（增益持续期）。
              - 设置 next_force_exit_time = 当前时间 + 5小时55分（增益期结束前 5 分钟强行出关）。
              - 设置 next_formation_time = 当前时间 + 12 小时（冷却期）。
              - 清空 next_formation_retry_time。
            """
            formation_time = formation_time or now_str()
            force_delay = 5 * 3600 + 55 * 60  # 5 小时 55 分钟
            self.set_avatar_state(avatar, "last_formation_time", formation_time)
            self.set_avatar_state(avatar, "formation_active_until", add_seconds_str(formation_time, 6 * 3600))
            self.set_avatar_state(avatar, "next_force_exit_time", add_seconds_str(formation_time, force_delay))
            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(formation_time, 12 * 3600))
            self.set_avatar_state(avatar, "next_formation_retry_time", "")
            log.info(
                f"Avatar [{avatar}] formation successful! Active until "
                f"{add_seconds_str(formation_time, 6 * 3600)}, "
                f"force exit at {add_seconds_str(formation_time, force_delay)}."
            )
            # 启动强行出关定时器
            force_delay_remaining = seconds_until(add_seconds_str(formation_time, force_delay))
            if force_delay_remaining > 0:
                asyncio.create_task(self.delayed_avatar_force_exit(avatar, force_delay_remaining))
                log.info(f"Avatar [{avatar}] force exit timer started: {int(force_delay_remaining)}s.")

    async def execute_avatar_formation(self, avatar):
            """
            化身版启阵流程（迁移自主循环 run_formation_meditation_loop 的启阵部分）。

            步骤：
            1. 检查冷却（12小时）和重试时间。
            2. 如果有待助阵邀请（其他化身发的），优先 .助阵。
            3. 否则发送 .启阵。
            4. 处理响应：直接成功 / 等待助阵(2分钟后查看编辑) / 冷却 / 未知。
            """
            a_state = self.get_avatar_state(avatar)
            last_formation = a_state.get("last_formation_time", "")
            next_retry = a_state.get("next_formation_retry_time", "")

            # 冷却检查：距上次启阵不足 12 小时
            if last_formation and is_future(add_seconds_str(last_formation, 12 * 3600)):
                # 冷却中，但如果有助阵邀请还是可以助阵
                if self.pending_formation_invite_msg:
                    await self._avatar_assist_formation(avatar)
                return

            # 重试时间检查
            if next_retry and is_future(next_retry):
                # 还没到重试时间，但如果有助阵邀请可以助阵
                if self.pending_formation_invite_msg:
                    await self._avatar_assist_formation(avatar)
                return

            # 优先助阵：如果有待助阵邀请，先助阵再考虑自己启阵
            if self.pending_formation_invite_msg:
                assisted = await self._avatar_assist_formation(avatar)
                if assisted:
                    return  # 助阵成功，不用自己启阵了

            log.info(f"Avatar [{avatar}] attempting Star Formation (.启阵)...")
            resp_msg = await self.send_and_wait_feedback_identity(
                avatar, ".启阵", timeout=120, return_response_msg=True, delete_after=False,
            )
            resp = (resp_msg.text or "") if resp_msg else ""

            # 修为不足处理
            if "修为不足" in resp:
                async def retry_formation():
                    resp_msg2 = await self.send_and_wait_feedback_identity(
                        avatar, ".启阵", timeout=120, return_response_msg=True, delete_after=False,
                    )
                    return resp_msg2
                success, resp = await self.handle_修为不足(avatar, retry_formation, cooldown_key="next_formation_time", cooldown_hours=2)
                if not success:
                    log.warning(f"Avatar [{avatar}] formation: 修为不足 after force exit, will retry next cycle")
                    return

            # 情况 1：直接成功
            if self.is_formation_success(resp):
                self.record_avatar_formation_success(avatar, self.message_effective_time_str(resp_msg))
                return

            # 情况 2：等待助阵（pending），存储邀请消息供其他化身助阵，等待 2 分钟查看编辑结果
            if self.is_formation_pending(resp):
                log.info(f"Avatar [{avatar}] formation pending, storing invite for other avatars...")
                self.pending_formation_invite_msg = resp_msg  # 存储邀请消息
                log.info(f"Avatar [{avatar}] waiting 2min for assist...")
                updated_msg = await self.get_updated_message(resp_msg, delay_sec=120)
                updated_resp = (updated_msg.text or "") if updated_msg else ""
                if self.is_formation_success(updated_resp):
                    self.pending_formation_invite_msg = None  # 清除
                    self.record_avatar_formation_success(avatar, self.message_effective_time_str(updated_msg))
                else:
                    log.info(f"Avatar [{avatar}] formation still pending after 2min. Retry in 10min.")
                    retry_at = add_seconds_str(now_str(), 600)
                    self.set_avatar_state(avatar, "next_formation_retry_time", retry_at)
                return

            # 情况 3：已有人启阵（"请勿重复操作"），尝试助阵
            if "请勿重复操作" in resp or "已发布" in resp:
                log.info(f"Avatar [{avatar}] formation already pending, trying to assist...")
                # 尝试获取邀请消息来助阵
                if self.pending_formation_invite_msg:
                    await self._avatar_assist_formation(avatar)
                else:
                    # 没有存储的邀请消息，10分钟后重试
                    retry_at = add_seconds_str(now_str(), 600)
                    self.set_avatar_state(avatar, "next_formation_retry_time", retry_at)
                    log.info(f"Avatar [{avatar}] no invite msg cached. Retry in 10min.")
                return

            # 情况 4：冷却或其他响应
            cd = self.parse_wait_time(resp)
            if cd >= 0 and any(k in resp for k in ["冷却", "再次启阵", "心神消耗"]):
                if cd == 0:
                    retry_after = 60
                    self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), retry_after))
                    self.set_avatar_state(avatar, "next_formation_retry_time", add_seconds_str(now_str(), retry_after))
                    log.info(f"Avatar [{avatar}] formation CD 0s. Retry in 60s.")
                    return
                # 真正的 CD（通常 12 小时），推算上次成功时间
                last_exec = datetime.now() + timedelta(seconds=cd) - timedelta(hours=12)
                self.set_avatar_state(avatar, "last_formation_time", dt_to_str(last_exec))
                self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd))
                self.set_avatar_state(avatar, "next_formation_retry_time", "")
                log.info(f"Avatar [{avatar}] formation on CD ({cd}s).")
                return

            # 情况 5：未知响应，20 分钟后重试
            if resp:
                notify_unrecognized_response(self, ".启阵", resp, log, f"启阵[{avatar}]")
            log.info(f"Avatar [{avatar}] formation unknown response. Retry in 20min.")
            retry_at = add_seconds_str(now_str(), 1200)
            self.set_avatar_state(avatar, "next_formation_retry_time", retry_at)

# =====================================================================
# 脚本入口
# =====================================================================
if __name__ == '__main__':
    """
    脚本入口。
    创建 Cultivator 实例并用 asyncio.run() 启动。
    所有异常被静默捕获（asyncio.run 的默认行为是打印 traceback，
    这里用 try/except 避免退出时显示 traceback，因为退出通常是\n"
    "手动的 Ctrl+C，不是错误）。
    """
    c = Cultivator()
    try:
        asyncio.run(c.start())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
