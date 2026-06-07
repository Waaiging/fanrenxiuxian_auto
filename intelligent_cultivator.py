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
STAR_GAZING_COMMAND_LEAD_SECONDS = 30
STAR_GAZING_SHIFT_LEAD_SECONDS = -25
STAR_GAZING_SHIFT_REPEAT_COUNT = 1
STAR_GAZING_SHIFT_REPEAT_INTERVAL_SECONDS = 3
STAR_GAZING_OPPORTUNITY_START_HOUR = 1
STAR_GAZING_DAILY_FALLBACK_HOUR = 23
STAR_GAZING_DAILY_FALLBACK_MINUTE = 59
STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
STAR_GAZING_ACTIVE_WINDOW_SECONDS = 59
STAR_GAZING_ROTATING_AVATARS = ["素缘子"]
STAR_GAZING_SHIFT_TARGET = "@Waaiging"

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

from telethon import TelegramClient, events  # Telegram 客户端框架，消息事件

# 导入各个功能模块（分离到不同文件中以降低本文件复杂度）
from auto_reply_features import is_auto_reply_followup, maybe_auto_reply_exchange
from common_command_features import CommonCommandMixin, common_command_default_state
from command_feedback import send_and_wait_feedback_common
from concubine_features import ConcubineMixin, concubine_default_state
from log_utils import (
    CommandLogFilter,          # 日志过滤器，过滤掉指令内容（保护隐私）
    cap_command_retries,       # 限制指令重试次数
    command_send_allowed,      # 检查是否允许发送指令
    command_send_precheck,     # 不记录发送次数的切换前预检
    handle_anti_bot_challenge, # 处理反机器人验证
    is_deep_meditation_ongoing_response,    # 判断是否为"正在深度闭关"的回复
    is_deep_meditation_settlement_response, # 判断是否为"闭关结算"的回复
    is_game_bot_sender,        # 判断消息发送者是否为游戏机器人
    is_not_deep_meditation_response,        # 判断是否为"未在闭关"的回复
    log_edited_message_if_needed,           # 记录编辑过的消息
    log_incoming_message,      # 记录收到的消息
    is_reply_to_manual_command,              # 检查是否为手动指令回复
    log_manual_outgoing_if_needed,          # 记录手动发送的消息
    log_mention_if_needed,     # 记录 @提及
    match_pending_edited_feedback,          # 安全匹配编辑后的机器人反馈
    notify_unrecognized_response,           # 通知无法识别的回复
    periodic_log_prune,        # 定期修剪日志文件
    prune_log_file,            # 修剪日志文件
    record_bot_no_response,    # 记录机器人无响应
    record_bot_response,       # 记录机器人响应
    record_cultivation_profile_from_text, # 同步境界/修为资料
    record_game_bot_activity,  # 记录游戏机器人活动
    record_manual_command_reply_state_if_needed, # 同步手动指令回复状态
    recent_profile_identity_for_text, # 识别无 reply 档案回复的身份
    remember_script_send_intent,  # 记录脚本发送意图
    remember_script_sent_message, # 记录脚本已发送的消息
    schedule_command_auto_delete, # 安排命令自动删除
    send_text_alert,           # 发送文本告警
    is_edited_message_for_current_account, # 判定消息是否针对当前账号的编辑
    feedback_response_conflicts, # 判定回复文本是否属于其他指令家族
    feedback_response_matches_command, # 判定回复文本是否正向匹配该指令
    feedback_response_requires_positive_match, # 已知指令需要正向内容匹配
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

# 各项活动的冷却时间（秒），这些值来自游戏设定
CLOUD_STAIRS_CD_SECONDS = 3 * 3600          # 登天阶冷却：3 小时
HEART_PLATFORM_MIN_INTERVAL_SECONDS = 3 * 3600  # 问心台最小间隔：3 小时
NINE_HEAVEN_WIND_CD_SECONDS = 12 * 3600     # 九天罡风冷却：12 小时
DAILY_TASK_START_HOUR = 7                   # 每日任务开始小时（北京时间早上 7 点）
DAILY_TASK_START_MINUTE = 0                 # 每日任务开始分钟
SECT_SKILL_MAX_DAILY = 3                    # 宗门传功每日上限次数
YUANYING_OUT_CD_SECONDS = 8 * 3600          # 元婴出窍冷却：8 小时
RIFT_SEARCH_CD_SECONDS = 12 * 3600          # 探寻裂缝冷却：12 小时
TREASURE_TOUCH_COMMAND = ".抚摸法宝 青竹蜂云剑（神雷版）"  # 抚摸法宝的具体指令
NURTURE_SPIRIT_COMMAND = ".温养器灵 青竹蜂云剑（神雷版）"  # 温养器灵指令
TREASURE_TOUCH_CD_SECONDS = 2 * 3600        # 抚摸法宝冷却：2 小时
SPIRIT_TREE_AVATAR = "缘生子"
SPIRIT_TREE_IRRIGATION_COMMAND = ".灵树灌溉"
SPIRIT_TREE_STATUS_COMMAND = ".灵树状态"
SPIRIT_TREE_HARVEST_COMMAND = ".采摘灵果"
SPIRIT_TREE_GUARD_COMMAND = ".协同守山"
SPIRIT_TREE_IRRIGATION_STATUS = "灌溉期"
SPIRIT_TREE_MATURE_STATUS = "成熟采摘期"
SPIRIT_TREE_MATURE_SECONDS = 24 * 3600
SPIRIT_TREE_MATURE_KEYWORDS = ("灵果已完全成熟", "采摘期开启", "成熟采摘期")
SPIRIT_TREE_GUARD_CD_SECONDS = 5 * 3600


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

class Cultivator(CommonCommandMixin, ConcubineMixin):
    """
    凌霄宫修仙主控类。
    继承自:
      - CommonCommandMixin: 通用固定冷却指令（如田野修炼、宗门战等）
      - ConcubineMixin: 侍妾相关操作

    职责: 管理所有修仙循环（登天阶、闭关、罡风、每日任务、元婴出窍等），
    通过 Telethon 客户端与 Telegram 游戏机器人交互。
    """

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
            "主魂": ["Waaiging"],
        }
        self.avatar_features = {
            "无咎子": {"meditation_prefix": ".推命", "training_cmd": ".野外历练", "training_level": "深入", "dream_map": True, "heart_trial": True, "tower": True, "daily_checkin": True},
            "缘生子": {"meditation_prefix": "", "training_cmd": ".野外历练", "training_level": "谨慎", "dream_map": True, "heart_trial": True, "tower": True, "spirit_tree_irrigation": True, "daily_checkin": True},
            "素缘子": {"meditation_prefix": "", "training_cmd": ".野外历练", "training_level": "谨慎", "dream_map": True, "heart_trial": True, "tower": True, "formation_assist": True, "star_gazing": True, "daily_checkin": True},
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
        self._spirit_tree_harvest_task = None      # 缘生子灵树成熟后的一次性采摘任务
        self._spirit_tree_guard_task = None        # 缘生子古剑门来袭后的一次性守山任务

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
                                s[k] = ""  # 过期的闭关时间清空，让脚本重新查询
                    # 修正状态标志同步
                    # 根据 deep_meditation_end_time 是否有效来同步 in_deep_meditation 标志
                    end_time = s.get("deep_meditation_end_time", "")
                    s["in_deep_meditation"] = bool(end_time and is_future(end_time))
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
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()

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
            # 安排自动删除（120 秒后删除，保持聊天清洁）
            schedule_command_auto_delete(self, msg, text=message, logger=log)
            log.info(f"🟢 OUT:\n{message}")
            return msg.id
        except Exception as e:
            log.error(f"Send Error [{message}] reply_to={target_reply}: {e}")
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

    async def _send_and_wait_feedback_raw(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True):
        """内部发送方法（不获取 avatar_send_lock，已被外部调用方持有）"""
        try:
            return await send_and_wait_feedback_common(
                self, log, message, timeout=timeout, max_retries=max_retries,
                reply_to=reply_to, return_msg=return_msg, return_response_msg=return_response_msg,
                delete_after=delete_after, return_msg_role="sent",
            )
        except Exception as e:
            log.error(f"_send_and_wait_feedback_raw [{message[:40]}] crashed: {e}")
            return None

    async def send_and_wait_feedback(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, force_identity_check=False):
        """
        发送指令并等待回复（带 avatar_send_lock 保护）。
        所有主魂业务通过此方法发送。如果当前身份不是主魂，或者主魂状态未确认，自动切回主魂再发送。
        """
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()

        yield_attempts = 0
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            async with self.avatar_send_lock:
                # 主魂自动身份对齐：如果当前是分身身份，或者主魂未确认，先切回主魂
                if force_identity_check or self.current_identity != "主魂" or not self._main_confirmed:
                    if not command_send_precheck(self, message, log, identity="主魂"):
                        log.info(f"Skip auto-switch to 主魂: main command is not sendable now ({message}).")
                        return None
                    if (
                        not force_identity_check
                        and self.current_identity in self.avatars
                    ):
                        wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                        if 0 <= wait_sec <= 60:
                            if yield_attempts == 0 or yield_attempts % 12 == 0:
                                log.info(
                                    f"Auto-switch to 主魂 deferred: {self.current_identity} "
                                    f"has commands due in {wait_sec:.1f}s."
                                )
                            yield_attempts += 1
                            should_yield = True
                            wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))

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
                        if resp_str:
                            if "成功" in resp_str or "已切换" in resp_str or "当前操控" in resp_str or "主魂" in resp_str:
                                is_success = True

                        if is_success:
                            self.current_identity = "主魂"
                            self._main_confirmed = True
                            log.info(f"✅ Auto-switch back to 主魂 confirmed.")
                            await asyncio.sleep(2)
                        else:
                            log.error(f"❌ Auto-switch back to 主魂 FAILED! Blocking main command: {message}. Response: {resp_str[:120]}")
                            return None

                if not should_yield:
                    resp = await self._send_and_wait_feedback_raw(
                        message, timeout=timeout, max_retries=max_retries,
                        reply_to=reply_to, return_msg=return_msg, return_response_msg=return_response_msg,
                        delete_after=delete_after,
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
        if not resp:
            return "unknown"
        # 模式1：回复包含明确计数 "今日已传功 1 / 3"
        count_match = re.search(r'今日已传功\s*\**\s*(\d+)\s*/\s*3', resp)
        if count_match:
            self.state["sect_skill_count"] = max(
                self.state.get("sect_skill_count", 0),
                int(count_match.group(1))
            )
            return "counted"
        # 模式2：今日次数已用完
        if any(k in resp for k in ["次数不足", "明日再来", "已经", "过于频繁"]):
            self.state["sect_skill_count"] = SECT_SKILL_MAX_DAILY
            return "done"
        # 模式3：回复目标无效（需要回复某条消息，但我们回复错了）
        if any(k in resp for k in ["失败", "需回复", "主魂"]):
            log.warning(f"Sect skill reply target invalid: {resp[:80]}...")
            return "invalid"
        # 模式4：传功成功
        if any(k in resp for k in ["成功", "元神", "传功", "玉简"]):
            self.state["sect_skill_count"] = min(
                SECT_SKILL_MAX_DAILY,
                self.state.get("sect_skill_count", 0) + 1
            )
            return "counted"
        # 无法识别的回复：记录到日志，但不要因此停止
        notify_unrecognized_response(self, ".宗门传功", resp, log, "宗门传功")
        return "unknown"

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
                _sender_id = getattr(msg, "sender_id", None)
                if _sender_id and _sender_id in self.pause_admins:
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

            # 如果是脚本自己手动发出的消息，记录但不处理
            if log_manual_outgoing_if_needed(self, msg, text=text):
                return
            if not sender_id:
                return

            sender_cache = None
            sender_cache = await event.get_sender()

            # 记录游戏机器人活动（用于判断机器人是否在线）
            if is_game_bot_sender(self, sender_cache):
                record_game_bot_activity(self, sender_cache, log)
                # 被动身份自愈 + 手动指令状态同步
                self.update_identity_passively(msg)
                manual_reply = is_reply_to_manual_command(self, msg)
                manual_processed = await record_manual_command_reply_state_if_needed(self, msg, text, sender_cache, log)
                if not manual_reply or not manual_processed:
                    self.maybe_record_spirit_tree_passive_message(msg, text, source="new message")
                if not manual_reply:
                    self.maybe_record_avatar_passive_states(msg)

                # 星宫化身专属：全天候被动截获好星相
                await self.maybe_handle_star_gazing_opportunity(msg, text, sender_cache)

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

            # ---- 反馈匹配（核心机制） ----
            is_matched = False

            # 1) 精确匹配：消息是回复（reply_to）我们发送的某条指令
            if msg.reply_to:
                replied_id = getattr(msg.reply_to, 'reply_to_msg_id', None)
                if replied_id in self.feedback_events:
                    # 过滤 bot 回声（bot 有时先回声再发实际回复）
                    cmd_text = self.feedback_commands.get(replied_id, "")
                    cmd_identity = getattr(self, "feedback_identities", {}).get(replied_id, getattr(self, "current_identity", "主魂"))
                    if text.strip() != cmd_text.strip():
                        # 内容校验：特定指令的 reply 必须通过内容匹配，防止游戏机器人 reply 到无关消息
                        _skip_reply = False
                        if mentions_other_user_for_identity(self, msg, text, cmd_identity):
                            log.warning(f"Reply match rejected for [{cmd_text}]: targets another user (msg {msg.id}): {text[:80]}")
                            _skip_reply = True
                        if feedback_response_conflicts(cmd_text, text):
                            if not feedback_response_matches_command(cmd_text, text):
                                log.warning(f"Reply match rejected for [{cmd_text}]: response family conflict (msg {msg.id}): {text[:80]}")
                                _skip_reply = True
                        if (
                            not _skip_reply
                            and feedback_response_requires_positive_match(cmd_text)
                            and not feedback_response_matches_command(cmd_text, text)
                        ):
                            log.warning(f"Reply match rejected for [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                            _skip_reply = True
                        if cmd_text == ".查看闭关" and not self.is_loose_feedback_candidate(cmd_text, text):
                            log.warning(f"Reply match rejected for [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                            _skip_reply = True
                        if not _skip_reply:
                            self.last_feedback_text[replied_id] = text
                            self.last_feedback_msg[replied_id] = msg
                            self.feedback_events[replied_id].set()
                            is_matched = True

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
        if "【凌霄云阶】" in stairs_resp or ("踏上" in stairs_resp and "云阶" in stairs_resp):
            self.update_cloud_stairs_progress_from_text(stairs_resp, source=source)
            self.update_completed_weeks_from_text(stairs_resp, source=source)

            now = now_str()
            self.state["last_stairs_time"] = now
            self.state["last_stairs_success_time"] = now
            self.state["next_stairs_time"] = add_seconds_str(now, CLOUD_STAIRS_CD_SECONDS)
            log.info(f"Cloud stairs success, next run at {self.state['next_stairs_time']}")
            return True

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
        如果 next_stairs_time 为空但 last_stairs_time 存在，
        用 last_stairs_time + 3小时 推断 next_stairs_time。

        这样即使状态文件丢失了 next_stairs_time，也能恢复 CD 信息。

        返回:
            str: next_stairs_time（原始或推断的）
        """
        last_stairs = self.state.get("last_stairs_time", "")
        next_stairs = self.state.get("next_stairs_time", "")
        if next_stairs:
            return next_stairs
        if last_stairs:
            inferred = add_seconds_str(last_stairs, CLOUD_STAIRS_CD_SECONDS)
            if is_future(inferred):
                self.state["next_stairs_time"] = inferred
                self.save_state()
                log.info(f"Cloud stairs next restored from last success: {inferred}")
                return inferred
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
        remaining = seconds_until(end_time)
        if remaining <= 0:
            return

        wait_sec = seconds_until(end_time) + random.randint(30, 60)
        if wait_sec > 0:
            log.info(f"{label}: waiting {wait_sec:.0f}s for deep meditation settlement window.")
            try:
                await asyncio.wait_for(self.meditation_state_event.wait(), timeout=wait_sec)
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
            log.info(f"Heart Platform check at {curr_step}/12, but Wind is ready. Sending .引九天罡风 first; Heart Platform is skipped.")
            wind_resp = await self.send_and_wait_feedback(".引九天罡风", timeout=120, force_identity_check=True)
            wind_pending = self.record_nine_heaven_wind_response(wind_resp, source="Cloud Stairs")
            await asyncio.sleep(3)

            # 如果罡风用了或者还是冷却完毕状态（说明施展失败），跳过问心台
            if wind_pending or self.is_nine_heaven_wind_ready():
                log.info("Heart Platform skipped to preserve Wind priority.")
                return

        # 使用问心台
        reason = "late daily fallback" if late_fallback and not (8 <= curr_step <= 11) else "late cloud-stairs climb"
        log.info(f"Progress {curr_step}/12, Wind unavailable, sending .问心台 for {reason}.")
        self.state["heart_platform_date"] = today
        self.state["last_heart_time"] = now_str()
        self.state["next_heart_time"] = add_seconds_str(f"{today} 00:05:00", 24 * 3600)
        self.save_state()

        hp_resp = await self.send_and_wait_feedback(".问心台", force_identity_check=True)
        if hp_resp:
            if any(k in hp_resp for k in ["问心台", "已经", "明天", "成功", "感受到", "感悟", "今日"]):
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
        # 化身循环活跃时等待
        while self._avatar_loop_active or self._avatar_loop_count > 0:
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
            should_use_wind = self.is_nine_heaven_wind_ready()
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
            await asyncio.sleep(wait_time + variance)

    # ------------------------------------------------------------------
    # 通用固定冷却指令处理
    # ------------------------------------------------------------------

    def record_fixed_cd_command_response(self, resp, command, last_key, next_key, cd_seconds):
        """
        通用固定冷却指令回复处理器。
        适用于"探寻裂缝"等有固定 CD 的指令。

        处理逻辑：
          1. 无回复 -> 10 分钟重试
          2. 有 CD 信息 -> 解析 CD 并设置下次执行时间
          3. 不可用但无 CD -> 10 分钟重试
          4. 成功（匹配成功关键词） -> 记录时间、设置 CD
          5. 无法识别 -> 10 分钟重试

        参数:
            resp: 游戏回复文本
            command: 指令文本（用于日志）
            last_key: 状态中上次成功时间的键名
            next_key: 状态中下次可执行时间的键名
            cd_seconds: 该指令的固定 CD 秒数

        返回:
            bool: 是否成功执行
        """
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{command}: response missing; retry scheduled at {self.state[next_key]}.")
            return False

        # 尝试解析冷却时间
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{command}: cooldown from response {cd}s, next at {self.state[next_key]}.")
            return False

        # 有冷却关键词但没解析出具体时间
        if any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{command}: unavailable but no cooldown parsed; retry at {self.state[next_key]}.")
            return False

        now = now_str()
        success_keywords = ["成功", "探寻", "裂缝", "收获", "空间", "发现"]
        if not any(k in resp for k in success_keywords):
            self.state[next_key] = add_seconds_str(now, 600)
            notify_unrecognized_response(self, command, resp, log, "固定冷却指令")
            log.warning(f"{command}: unrecognized response; skipped and retry scheduled at {self.state[next_key]}.")
            return False

        # 成功执行
        self.state[last_key] = now
        self.state[next_key] = add_seconds_str(now, cd_seconds)
        log.info(f"{command}: recorded success/response, next at {self.state[next_key]}.")
        return True

    # ------------------------------------------------------------------
    # 元婴出窍响应处理
    # ------------------------------------------------------------------

    def record_yuanying_out_start_response(self, resp):
        """
        解析元婴出窍的回复并更新状态。

        响应模式：
          1. 冷却中 -> 解析 CD，清除活跃状态
          2. 成功出窍（含"元婴出窍"、"神游"等关键词）
             -> 设置出窍结束时间、标记为活跃
          3. 明确失败 -> 不告警，按 2 小时周期后再试，并使主魂确认失效
          4. 无法识别 -> 10 分钟重试

        参数:
            resp: 游戏回复文本

        返回:
            bool: 是否成功出窍
        """
        if not resp:
            self.state["next_yuanying_out_time"] = add_seconds_str(now_str(), 600)
            log.warning(f".元婴出窍: response missing; retry at {self.state['next_yuanying_out_time']}.")
            return False

        # 冷却中
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now_str(), cd)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            log.info(f".元婴出窍: cooldown from response {cd}s, next at {self.state['next_yuanying_out_time']}.")
            return False

        now = now_str()
        if any(k in resp for k in ["尚未凝聚元婴", "无法施展此术"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now, 6 * 3600)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            log.info(f".元婴出窍 unavailable: next check at {self.state['next_yuanying_out_time']}.")
            return False
        # 成功出窍
        if not any(k in resp for k in ["元婴出窍", "神游", "云游", "出窍", "自动结算"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now, 600)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            notify_unrecognized_response(self, ".元婴出窍", resp, log, "元婴出窍")
            log.warning(f".元婴出窍: unrecognized response; skipped until {self.state['next_yuanying_out_time']}.")
            return False

        # 使用 CD（优先用解析到的，没有则用默认 8 小时）
        cd = cd if cd > 0 else YUANYING_OUT_CD_SECONDS
        self.state["last_yuanying_out_time"] = now
        self.state["next_yuanying_out_time"] = add_seconds_str(now, cd)
        self.state["yuanying_out_end_time"] = self.state["next_yuanying_out_time"]
        self.state["yuanying_out_active"] = True
        log.info(f".元婴出窍: started, auto-return due at {self.state['yuanying_out_end_time']}.")
        return True

    # ------------------------------------------------------------------
    # 抚摸法宝响应处理
    # ------------------------------------------------------------------

    def record_treasure_touch_response(self, resp):
        """
        解析抚摸法宝的回复并更新状态。

        响应模式：
          1. 冷却中（"休息"、"冷却"等关键词 + 时间）
             -> 解析 CD 并设置下次时间
          2. 成功（"联系更加紧密"、"器灵传来了喜悦"等）
             -> 记录成功时间，设置 2 小时 CD
          3. 无法识别 -> 10 分钟重试

        参数:
            resp: 游戏回复文本

        返回:
            bool: 是否成功抚摸
        """
        command = TREASURE_TOUCH_COMMAND
        next_key = "next_treasure_touch_time"
        last_key = "last_treasure_touch_time"
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{command}: response missing; retry scheduled at {self.state[next_key]}.")
            return False

        # 冷却中
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需", "互动"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{command}: cooldown from response {cd}s, next at {self.state[next_key]}.")
            return False

        # 成功
        if any(k in resp for k in ["联系更加紧密", "器灵传来了喜悦", "默契", "经验", "与它互动"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, TREASURE_TOUCH_CD_SECONDS)
            log.info(f"{command}: recorded success, next at {self.state[next_key]}.")
            return True

        if (
            "没有这件拥有器灵的法宝" in resp
            or "名字输入错误" in resp
            or ("没有这件" in resp and "器灵" in resp)
        ):
            now = now_str()
            self.state["last_treasure_touch_error"] = resp[:200]
            self.state["last_treasure_touch_error_time"] = now
            self.state[next_key] = add_seconds_str(now, TREASURE_TOUCH_CD_SECONDS)
            self._main_confirmed = False
            log.warning(
                f"{command}: definite failure ({resp[:80]}), next at "
                f"{self.state[next_key]}; main identity will be re-confirmed."
            )
            return False

        # 无法识别
        self.state[next_key] = add_seconds_str(now_str(), 600)
        notify_unrecognized_response(self, command, resp, log, "抚摸法宝")
        log.warning(f"{command}: unrecognized response; skipped and retry scheduled at {self.state[next_key]}.")
        return False

    # ------------------------------------------------------------------
    # 裂缝虚弱检测 & 紧急停止
    # ------------------------------------------------------------------

    def is_rift_weakness_response(self, text):
        """
        判断回复是否表示触发了元婴虚弱期。

        虚弱期是探寻裂缝时的一种负面状态，会让角色虚弱无法操作。
        检测到后需要紧急停止脚本并通知用户。
        因为虚弱期需要用户手动处理（可能是吃药、等待等），脚本无法自动处理。

        参数:
            text: 游戏回复文本

        返回:
            bool: 是否检测到虚弱期
        """
        if not text:
            return False
        clean = text.replace("**", "").replace(" ", "")
        return (
            "元婴遁逃·虚弱" in clean
            or ("虚弱期" in clean and "无法进行夺舍" in clean)
            or ("神魂遭受重创" in clean and "虚弱" in clean)
        )

    async def stop_for_rift_weakness(self, response):
        """
        检测到元婴虚弱期时的紧急停止流程：
          1. 清空下次探寻裂缝时间（防止重启后继续尝试）
          2. 发送告警通知用户
          3. 停止整个脚本（is_running = False）

        参数:
            response: 触发停止的回复文本（附带在告警中供用户参考）
        """
        self.state["next_rift_search_time"] = ""
        self.save_state()
        await send_text_alert(
            self,
            "凌霄宫探寻裂缝告警",
            "探寻裂缝触发元婴虚弱期，脚本已停止，请手动处理。\n\n"
            f"机器人回复：\n{response}",
            log,
        )
        log.critical(f"Rift weakness detected. Stopping main script:\n{response}")
        self.is_running = False

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
            await self._wait_for_main_identity()
            end_time = self.state.get("yuanying_out_end_time") or self.state.get("next_yuanying_out_time", "")
            active = self.state.get("yuanying_out_active")

            # 情况1：在外活跃，等待归窍
            if active and end_time and is_future(end_time):
                wait_time = seconds_until(end_time)
                log.info(f"Yuanying out active. Auto-return due at {end_time}.")
                await asyncio.sleep(min(wait_time, 600))
                continue

            # 情况2：在外活跃但归窍时间已到，执行归窍
            if active:
                log.info("Yuanying out time expired. Auto-resetting state.")
                # 元婴自动归窍，直接重置状态
                self.state["yuanying_out_active"] = False
                self.state["yuanying_out_end_time"] = ""
                self.save_state()
                await asyncio.sleep(5)

            # 情况3：CD 未到
            next_time = self.state.get("next_yuanying_out_time", "")
            if next_time and is_future(next_time):
                await asyncio.sleep(min(seconds_until(next_time), 600))
                continue

            # 情况4：CD 已到，重新出窍
            log.info("Yuanying ability due: sending .元婴出窍.")
            resp = await self.send_and_wait_feedback(".元婴出窍", timeout=120)
            self.record_yuanying_out_start_response(resp)
            self.save_state()
            await asyncio.sleep(5)

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
        command = ".探寻裂缝"
        last_key = "last_rift_search_time"
        next_key = "next_rift_search_time"

        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get(next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Rift search loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(min(wait_time, 600))
                continue

            log.info("Rift search due: sending .探寻裂缝.")
            resp_msg = await self.send_and_wait_feedback(command, timeout=120, return_response_msg=True)
            if resp_msg is None:
                if await self.sleep_after_blocked_command(command, "Rift search"):
                    continue
                log.info("Rift search: no response received, retrying later.")
                await asyncio.sleep(600)
                continue
            resp_text = resp_msg.text or ""

            # 检测元婴虚弱期
            if self.is_rift_weakness_response(resp_text):
                # 验证：确保回复确实是对我们消息的回复
                replied_id = None
                if hasattr(resp_msg, 'reply_to') and resp_msg.reply_to:
                    replied_id = getattr(resp_msg.reply_to, 'reply_to_msg_id', None)
                elif hasattr(resp_msg, 'reply_to_msg_id'):
                    replied_id = resp_msg.reply_to_msg_id
                if replied_id and replied_id != getattr(self, 'last_sent_id', None):
                    # 回复的目标不是我们的消息，可能是别人的虚弱期
                    log.warning(f"Rift weakness detected but reply_to #{replied_id} != our sent msg, likely someone else's. Skipping.")
                    continue
                # 确认是我们自己的虚弱期，停止脚本
                await self.stop_for_rift_weakness(resp_text)
                break

            self.record_fixed_cd_command_response(resp_text, command, last_key, next_key, RIFT_SEARCH_CD_SECONDS)
            self.save_state()
            wait_time = seconds_until(self.state.get(next_key, "")) or 600
            await asyncio.sleep(min(wait_time, 600))

    # ------------------------------------------------------------------
    # 抚摸法宝循环
    # ------------------------------------------------------------------

    async def run_treasure_touch_loop(self):
        """
        定时抚摸本命法宝器灵循环。
        定时（默认 2 小时冷却）发送 ".抚摸法宝 青竹蜂云剑" 指令，
        提升法宝与主人的亲密度/默契度。
        仅在主魂身份时执行，化身身份时跳过等待。
        """
        await self.startup_done.wait()
        while self.is_running:
            # 等待正在发送的化身指令完成
            await self._wait_for_main_identity()
            next_time = self.state.get("next_treasure_touch_time", "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Treasure touch loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(min(wait_time, 600))
                continue

            log.info(f"Treasure touch due: sending {TREASURE_TOUCH_COMMAND}.")
            resp = await self.send_and_wait_feedback(
                TREASURE_TOUCH_COMMAND,
                timeout=90,
                force_identity_check=True,
            )
            self.record_treasure_touch_response(resp)
            self.save_state()
            wait_time = seconds_until(self.state.get("next_treasure_touch_time", "")) or 600
            await asyncio.sleep(min(wait_time, 600))


    def record_nurture_spirit_response(self, resp):
        """解析温养器灵的回复并更新状态（6小时冷却）"""
        command = NURTURE_SPIRIT_COMMAND
        next_key = "next_nurture_spirit_time"
        last_key = "last_nurture_spirit_time"
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{command}: response missing; retry at {self.state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{command}: cooldown {cd}s, next at {self.state[next_key]}.")
            return False

        if any(k in resp for k in ["温养", "默契", "经验", "成功", "提升", "喜悦"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, 6 * 3600)
            log.info(f"{command}: success, next at {self.state[next_key]}.")
            return True

        notify_unrecognized_response(self, command, resp, log, "温养器灵")
        self.state[next_key] = add_seconds_str(now_str(), 600)
        log.warning(f"{command}: unrecognized; retry at {self.state[next_key]}.")
        return False

    
    # ------------------------------------------------------------------
    # 温养器灵循环
    # ------------------------------------------------------------------

    async def run_nurture_spirit_loop(self):
        """定时温养器灵循环（6小时冷却）"""
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get("next_nurture_spirit_time", "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Nurture spirit loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(min(wait_time, 600))
                continue

            log.info(f"Nurture spirit due: sending {NURTURE_SPIRIT_COMMAND}.")
            resp = await self.send_and_wait_feedback(NURTURE_SPIRIT_COMMAND, timeout=90)
            self.record_nurture_spirit_response(resp)
            self.save_state()
            wait_time = seconds_until(self.state.get("next_nurture_spirit_time", "")) or 600
            await asyncio.sleep(min(wait_time, 600))

        # ------------------------------------------------------------------
    # 登天阶循环（主循环之一）
    # ------------------------------------------------------------------

    async def run_cloud_stairs_loop(self):
        """
        凌霄宫云阶问心循环。
        这是最主要的玩法循环之一，管理登天阶和问心台的使用策略。

        循环逻辑：
          1. 天阶状态检查（缓存缺失时才查，减少不必要的 API 调用）
             - 解析登阶冷却、九天罡风冷却
          2. 登天阶执行（CD 到了就登）
             - 登阶前判断是否需要先用问心台
          3. 问心台每日保底（23:50 强制使用）
          4. 计算等待时间

        问心台使用策略（在 maybe_use_heart_platform_before_climb 中实现）：
          - 高阶（8-11 阶）优先用问心台辅助
          - 罡风 buff 优先于问心台 buff
          - 23:50 保底使用
        """
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            # ---- 1. 天阶状态检查 ----
            # 天阶状态只作为缓存缺失时的补账；正常登阶按固定 3 小时 CD 执行。
            next_stairs = self.restore_cloud_stairs_time_from_last()
            status_missing = not self.state.get("cloud_stairs_progress")
            if status_missing:
                log.info("Checking .天阶状态 (missing cached progress)...")
                status_resp = await self.send_and_wait_feedback(".天阶状态", force_identity_check=True)
            else:
                log.info(f"Cloud stairs cache valid. Skipping .天阶状态. Next Stairs: {next_stairs}")
                status_resp = None
            if status_resp:
                self.update_cloud_stairs_progress_from_text(status_resp, source="Cloud stairs status")
                self.update_completed_weeks_from_text(status_resp, source="Cloud stairs status")

                # 解析登阶冷却时间
                cd = self.parse_wait_time(status_resp, line_identifier="登阶冷却")
                if cd > 0:
                    self.state["next_stairs_time"] = add_seconds_str(now_str(), cd)
                    log.info(f"Cloud stairs CD: {cd}s, next run at {self.state['next_stairs_time']}")
                elif "可立即登阶" in status_resp:
                    self.state["next_stairs_time"] = ""
                    log.info("Cloud stairs ready immediately")

                # 解析引九天罡风冷却时间（从天阶状态中顺带解析，减少单独查询）
                wind_cd = self.parse_wait_time(status_resp, line_identifier="引九天罡风")
                if wind_cd > 0:
                    self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now_str(), wind_cd)
                    log.info(f"Nine Heaven Wind CD: {wind_cd}s, next run at {self.state['nine_heaven_wind_cd_time']}")
                elif "可立即施展" in status_resp or "引九天罡风" not in status_resp:
                    # 如果没有冷却时间或未解锁引九天罡风，设置为 0 表示可用
                    self.state["nine_heaven_wind_cd_time"] = 0

                self.save_state()

            # ---- 2. 登天阶执行 ----
            next_time_str = self.state.get("next_stairs_time", "")
            curr_step = self.get_cloud_stairs_step()
            today = datetime.now().strftime('%Y-%m-%d')

            if not next_time_str or not is_future(next_time_str):
                # 登阶前先考虑是否用问心台
                await self.maybe_use_heart_platform_before_climb(curr_step, today)

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
                await self.maybe_use_heart_platform_before_climb(curr_step, today, allow_daily_fallback=True)

            # ---- 3. 计算等待时间 ----
            next_stairs_str = self.state.get("next_stairs_time", "")
            wait_time = random.randint(10, 20)  # 如果 CD 到了，默认只睡一小会儿

            if next_stairs_str and is_future(next_stairs_str):
                wait_time = seconds_until(next_stairs_str) + random.randint(5, 15)
                log.info(f"Stairs CD active. Sleeping {wait_time}s until {next_stairs_str}")
            else:
                log.info(f"Stairs ready or no CD. Short sleep {wait_time}s before next attempt.")
                log.info(f"Cloud stairs next: {next_stairs_str}")

            # ---- 4. 问心台每日保底调度 ----
            # 如果今天还没用问心台，检查是否需要提前醒来执行保底
            if self.state.get("heart_platform_date") != today:
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
            await asyncio.sleep(wait_time + variance)

    # ------------------------------------------------------------------
    # 每日任务循环
    # ------------------------------------------------------------------

    async def run_daily_tasks(self):
        """
        每日任务循环。
        每天早上 7:00 执行一次，完成以下任务：
          1. 闯塔（.闯塔，配合 .借天门势）
          2. 宗门点卯（.宗门点卯）

        实现细节：
          - 使用 state["done"] 记录今日已完成的任务，防止重复执行
          - 闯塔前先 .借天门势（增加成功率）
        """
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            now = datetime.now()
            daily_wait = seconds_until_daily_task_start(now)
            if daily_wait > 0:
                next_run = now + timedelta(seconds=daily_wait)
                log.info(f"Daily tasks paused before {daily_task_start_label()}. Next check at {dt_to_str(next_run)}.")
                await asyncio.sleep(daily_wait + random.randint(0, 30))
                continue

            today = now.strftime('%Y-%m-%d')

            # 每日状态重置：每当日期变化时重置
            if self.state.get("date") != today:
                log.info(f"New Day Detected ({today}): Resetting state.")
                self.state["date"] = today
                self.state["done"] = []
                self.state["sect_skill_count"] = 0
                if self.state.get("heart_platform_date") != today:
                    self.state["heart_platform_date"] = ""
                self.save_state()

            # ---- 1. 执行每日任务（闯塔、宗门点卯） ----
            dianmao_msg_id = None
            for t in [".闯塔", ".宗门点卯"]:
                if t not in self.state["done"]:
                    # 预先占位，防止高频重复（即使执行失败也不会反复重试）
                    self.state["done"].append(t)
                    self.save_state()

                    if t == ".闯塔":
                        # 闯塔前先借天门势（增加成功率）
                        await self.send_and_wait_feedback(".借天门势")
                        await asyncio.sleep(5)

                    sent_msg = await self.send_and_wait_feedback(t, return_msg=True)
                    if sent_msg:
                        if t == ".宗门点卯":
                            # 保存点卯消息的 ID，后续传功需要回复这条消息
                            self.state["last_dianmao_msg_id"] = sent_msg.id
                        self.save_state()
                    await asyncio.sleep(5)

            await asyncio.sleep(600)  # 等 10 分钟再检查（防止 7:00 前几秒就跳过了）

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
        retry_time = self.state.get("next_meditation_retry_time", "")
        if retry_time and is_future(retry_time):
            log.info(f"Meditation: deferred after unknown response until {retry_time}.")
            await asyncio.sleep(seconds_until(retry_time))
            self.state["next_meditation_retry_time"] = ""
            self.save_state()

        # === 启动恢复：检查是否处于深度闭关中 ===
        if self.state.get("in_deep_meditation"):
            end_time = self.state.get("deep_meditation_end_time", "")
            if end_time and is_future(end_time):
                await self.sleep_until_meditation_check(end_time, label="Startup recovery")
            else:
                log.info("Startup recovery: 8h passed, entering checking phase.")

        async def settle_and_restart_meditation(reason):
            """Settle completed meditation and immediately start the next deep meditation."""
            log.info(f"Meditation Step 4: {reason}. Settling and restarting immediately...")
            await self.send_and_wait_feedback(".闭关修炼")
            await asyncio.sleep(3)
            med_resp = await self.send_and_wait_feedback(".深度闭关")
            med_text = getattr(med_resp, "text", "") if hasattr(med_resp, "text") else med_resp if isinstance(med_resp, str) else str(med_resp) if med_resp else ""
            cd = self.parse_wait_time(med_text)
            if cd > 0 or self.is_deep_meditation_start_success(med_text):
                self.state["in_deep_meditation"] = True
                self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600)
                self.state["last_deep_meditation_time"] = now_str()
                self.state["last_deep_date"] = datetime.now().strftime('%Y-%m-%d')
                self.state["next_meditation_retry_time"] = ""
                self.save_state()
                await self.place_concubine_after_meditation_start()
                log.info(f"Meditation: restarted, end at {self.state['deep_meditation_end_time']}.")
                return
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
            retry_time = self.state.get("next_meditation_retry_time", "")
            if retry_time and is_future(retry_time):
                log.info(f"Meditation: deferred after unknown response until {retry_time}.")
                await asyncio.sleep(seconds_until(retry_time))
                self.state["next_meditation_retry_time"] = ""
                self.save_state()
                continue

            # 如果当前状态已经是"深度闭关中"，直接跳到监控步骤（Step 3）
            if not self.state.get("in_deep_meditation"):
                # === Step 1: 发送 .深度闭关 ===
                log.info("Meditation Step 1: Sending .深度闭关...")
                resp = await self.send_and_wait_feedback(".深度闭关")
                cd = self.parse_wait_time(resp)

                # 识别到"已在"或开启成功，进入闭关监控状态
                if cd > 0 or ("已在" in resp and "深度闭关" in resp):
                    if cd > 0:
                        # Step 2: 开启成功，睡足 8 小时
                        self.state["in_deep_meditation"] = True
                        self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd)
                        self.state["last_deep_meditation_time"] = now_str()
                        self.state["last_deep_date"] = datetime.now().strftime('%Y-%m-%d')
                        self.save_state()
                        await self.place_concubine_after_meditation_start()
                        log.info(f"Meditation Step 2: Started, end at {self.state['deep_meditation_end_time']}, sleeping 8 hours...")
                        await self.sleep_until_meditation_check(self.state["deep_meditation_end_time"])
                    else:
                        # "已在深度闭关"（之前就在闭关中）
                        log.info("Meditation: Already in deep meditation, moving to Step 3.")
                        self.state["in_deep_meditation"] = True
                        self.save_state()
                        await self.place_concubine_after_meditation_start()
                else:
                    # Step 1 失败：被游戏拒绝
                    if resp and any(k in resp for k in ["冷却", "后再", "无法", "尚未", "普通闭关", "闭关修炼", "先闭关"]):
                        # 启动被明确拦截 -> 通过 .闭关修炼 保底激活
                        log.info("Meditation: Start blocked, activating via .闭关修炼...")
                        await self.send_and_wait_feedback(".闭关修炼")
                        await asyncio.sleep(60)
                    else:
                        # 无法识别的回复
                        if resp:
                            notify_unrecognized_response(self, ".深度闭关", resp, log, "深度闭关")
                        self.state["in_deep_meditation"] = False
                        self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600)
                        self.save_state()
                        await asyncio.sleep(600)
                    continue

            # === Step 3 & 4: 监控与结算 ===
            while self.is_running and self.state.get("in_deep_meditation"):
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
                    self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd_check)
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
                    await asyncio.sleep(600)
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
        msg_dt = getattr(msg, "date", None)
        if not msg_dt:
            return 0
        try:
            now_dt = datetime.now(msg_dt.tzinfo) if msg_dt.tzinfo else datetime.utcnow()
            return max(0, (now_dt - msg_dt).total_seconds())
        except Exception:
            return 0

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
            "next_field_training_time": "", "nickname": "", "in_deep_meditation": False,
            "deep_meditation_end_time": "", "last_tower_date": "",
            "next_dream_map_time": "", "next_heart_trial_time": "",
            "next_concubine_voyage_time": "", "last_concubine_voyage_time": "",
            "concubine_voyage_active": False,
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_status": SPIRIT_TREE_IRRIGATION_STATUS, "spirit_tree_mature_until": "",
            "spirit_tree_harvested_in_mature_period": False, "spirit_tree_harvest_attempted_in_mature_period": False,
            "spirit_tree_harvest_pending": False, "spirit_tree_last_harvest_time": "",
            "spirit_tree_last_mature_detected_time": "", "spirit_tree_last_mature_msg_id": 0,
            "spirit_tree_last_status_time": "",
            "spirit_tree_invasion_status": "", "spirit_tree_guard_pending": False,
            "next_spirit_tree_guard_time": "", "last_spirit_tree_guard_time": "",
            "spirit_tree_last_invasion_time": "", "spirit_tree_last_invasion_msg_id": 0,
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

        # 以下是被动状态检测方法（从万灵宗移植）
        # 用于监控手动发送指令的结果，及时同步到 state

    def clean_spirit_tree_text(self, text):
        return str(text or "").replace("**", "").strip()

    def spirit_tree_text_indicates_mature(self, text):
        clean = self.clean_spirit_tree_text(text)
        if any(k in clean for k in SPIRIT_TREE_MATURE_KEYWORDS):
            return True
        return (
            "采摘期" in clean
            and any(k in clean for k in ["剩余", "结束", "已开启", "开启中", "可采摘"])
            and "可查看神树成熟进度" not in clean
        )

    def spirit_tree_text_indicates_invasion(self, text):
        clean = self.clean_spirit_tree_text(text)
        if not clean or "古剑门" not in clean:
            return False
        if any(k in clean for k in ["暂息旧隙", "试剑修枝", "顺手替灵树斩去乱枝"]):
            return False
        return any(k in clean for k in ["古剑门来袭", "古剑门·攻山夺枝", "突袭山门", "强夺本轮枝果", "攻山夺枝"])

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

    def parse_spirit_tree_mature_seconds(self, text):
        clean = self.clean_spirit_tree_text(text)
        for line in clean.splitlines():
            if "采摘期" in line or "灵果已完全成熟" in line:
                cd = self.parse_wait_time(line)
                if cd > 0:
                    return cd, True
        cd = self.parse_wait_time(clean)
        if cd > 0 and any(k in clean for k in ["采摘期剩余", "采摘期结束", "成熟采摘期"]):
            return cd, True
        return SPIRIT_TREE_MATURE_SECONDS, False

    def normalize_spirit_tree_state(self, avatar=SPIRIT_TREE_AVATAR):
        a_state = self.get_avatar_state(avatar)
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
                log.info(f"[{avatar}] spirit tree mature period ended; resume irrigation.")
        if changed:
            self.save_state()
        return a_state

    def record_spirit_tree_mature_state(self, text, msg=None, source=""):
        avatar = SPIRIT_TREE_AVATAR
        a_state = self.normalize_spirit_tree_state(avatar)
        seconds, parsed_from_status = self.parse_spirit_tree_mature_seconds(text)
        msg_id = getattr(msg, "id", None) or 0
        same_msg = bool(msg_id and a_state.get("spirit_tree_last_mature_msg_id") == msg_id)
        old_until = a_state.get("spirit_tree_mature_until", "")
        already_mature = a_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS and old_until and is_future(old_until)
        candidate_until = add_seconds_str(now_str(), seconds)

        if already_mature and same_msg:
            mature_until = old_until
        elif already_mature and parsed_from_status:
            mature_until = candidate_until
        elif already_mature:
            mature_until = old_until
        else:
            mature_until = candidate_until
            a_state["spirit_tree_harvested_in_mature_period"] = False
            a_state["spirit_tree_harvest_attempted_in_mature_period"] = False

        a_state["spirit_tree_status"] = SPIRIT_TREE_MATURE_STATUS
        a_state["spirit_tree_mature_until"] = mature_until
        a_state["next_spirit_tree_irrigation_time"] = mature_until
        a_state["spirit_tree_last_mature_detected_time"] = now_str()
        a_state["spirit_tree_last_mature_msg_id"] = msg_id
        if SPIRIT_TREE_STATUS_COMMAND in self.clean_spirit_tree_text(text) or "采摘期" in self.clean_spirit_tree_text(text):
            a_state["spirit_tree_last_status_time"] = now_str()

        needs_harvest = not (
            a_state.get("spirit_tree_harvested_in_mature_period")
            or a_state.get("spirit_tree_harvest_attempted_in_mature_period")
        )
        a_state["spirit_tree_harvest_pending"] = needs_harvest
        self.save_state()
        log.info(f"[{avatar}] spirit tree status -> {SPIRIT_TREE_MATURE_STATUS} until {mature_until} ({source}).")
        return needs_harvest

    def record_spirit_tree_invasion_state(self, text, msg=None, source=""):
        avatar = SPIRIT_TREE_AVATAR
        a_state = self.get_avatar_state(avatar)
        now = now_str()
        next_guard = a_state.get("next_spirit_tree_guard_time", "")
        if next_guard and is_future(next_guard):
            a_state["spirit_tree_invasion_status"] = ""
            a_state["spirit_tree_guard_pending"] = False
            a_state["spirit_tree_last_invasion_time"] = now
            a_state["spirit_tree_last_invasion_msg_id"] = getattr(msg, "id", None) or 0
            self.save_state()
            log.info(f"[{avatar}] 古剑门来袭 ignored during guard cooldown until {next_guard} ({source}).")
            return False
        a_state["spirit_tree_invasion_status"] = "古剑门来袭"
        a_state["spirit_tree_guard_pending"] = True
        a_state["spirit_tree_last_invasion_time"] = now
        a_state["spirit_tree_last_invasion_msg_id"] = getattr(msg, "id", None) or 0
        self.save_state()
        log.info(f"[{avatar}] 古剑门来袭 detected ({source}); scheduling {SPIRIT_TREE_GUARD_COMMAND}.")
        return True

    def record_spirit_tree_harvest_response(self, text):
        avatar = SPIRIT_TREE_AVATAR
        a_state = self.get_avatar_state(avatar)
        clean = self.clean_spirit_tree_text(text)
        if any(k in clean for k in ["灵果入腹", "摘下一枚", "采摘成功", "获得", "修为增长", "已经采摘", "已采摘", "本轮已采"]):
            a_state["spirit_tree_harvested_in_mature_period"] = True
            a_state["spirit_tree_harvest_pending"] = False
            a_state["spirit_tree_last_harvest_time"] = now_str()
            self.save_state()
            log.info(f"[{avatar}] spirit tree harvest recorded.")
            return True
        if any(k in clean for k in ["尚未成熟", "还未成熟", "未成熟", "采摘期未开启", "尚未进入采摘期"]):
            a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
            a_state["spirit_tree_mature_until"] = ""
            a_state["spirit_tree_harvested_in_mature_period"] = False
            a_state["spirit_tree_harvest_attempted_in_mature_period"] = False
            a_state["spirit_tree_harvest_pending"] = False
            a_state["next_spirit_tree_irrigation_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            log.info(f"[{avatar}] spirit tree harvest rejected as not mature; resume irrigation checks.")
            return True
        if clean:
            notify_unrecognized_response(self, SPIRIT_TREE_HARVEST_COMMAND, clean, log, "灵树采摘")
        return False

    def record_spirit_tree_irrigation_state(self, text, source=""):
        avatar = SPIRIT_TREE_AVATAR
        a_state = self.get_avatar_state(avatar)
        clean = self.clean_spirit_tree_text(text)
        previous_status = a_state.get("spirit_tree_status", "")
        a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
        a_state["spirit_tree_mature_until"] = ""
        a_state["spirit_tree_harvested_in_mature_period"] = False
        a_state["spirit_tree_harvest_attempted_in_mature_period"] = False
        a_state["spirit_tree_harvest_pending"] = False
        if "灵眼之树" in clean or "灵树状态" in clean:
            a_state["spirit_tree_last_status_time"] = now_str()

        cd = self.parse_wait_time(clean)
        if cd > 0:
            a_state["next_spirit_tree_irrigation_time"] = add_seconds_str(now_str(), cd)
        elif "灵树灌溉" in clean and "成熟度" in clean:
            a_state["next_spirit_tree_irrigation_time"] = add_seconds_str(now_str(), 2 * 3600)
        elif previous_status == SPIRIT_TREE_MATURE_STATUS:
            a_state["next_spirit_tree_irrigation_time"] = ""

        self.save_state()
        log.info(f"[{avatar}] spirit tree status -> {SPIRIT_TREE_IRRIGATION_STATUS} ({source}).")
        return True

    def record_spirit_tree_guard_response(self, text):
        avatar = SPIRIT_TREE_AVATAR
        a_state = self.get_avatar_state(avatar)
        clean = self.clean_spirit_tree_text(text)
        cd = self.parse_wait_time(clean)
        cd_seconds = cd if cd > 0 else SPIRIT_TREE_GUARD_CD_SECONDS
        a_state["last_spirit_tree_guard_time"] = now_str()
        a_state["next_spirit_tree_guard_time"] = add_seconds_str(now_str(), cd_seconds)
        a_state["spirit_tree_invasion_status"] = ""
        a_state["spirit_tree_guard_pending"] = False
        self.save_state()
        if clean and not any(k in clean for k in ["协同守山", "护山", "古剑门", "冷却", "已协同", "加固", "守山"]):
            notify_unrecognized_response(self, SPIRIT_TREE_GUARD_COMMAND, clean, log, "协同守山")
        log.info(f"[{avatar}] spirit tree guard recorded; next guard after {self.get_avatar_state(avatar).get('next_spirit_tree_guard_time', '')}.")
        return True

    def schedule_spirit_tree_harvest_once(self, reason="mature"):
        task = getattr(self, "_spirit_tree_harvest_task", None)
        if task and not task.done():
            return
        self._spirit_tree_harvest_task = asyncio.create_task(self.execute_spirit_tree_harvest_once(reason))

    def schedule_spirit_tree_guard_once(self, reason="invasion"):
        task = getattr(self, "_spirit_tree_guard_task", None)
        if task and not task.done():
            return
        self._spirit_tree_guard_task = asyncio.create_task(self.execute_spirit_tree_guard_once(reason))

    def maybe_record_spirit_tree_passive_message(self, msg, text, source="passive"):
        indicates_mature = self.spirit_tree_text_indicates_mature(text)
        indicates_irrigation = self.spirit_tree_text_indicates_irrigation_state(text)
        indicates_invasion = self.spirit_tree_text_indicates_invasion(text)
        if (
            (indicates_mature or indicates_irrigation or indicates_invasion)
            and not self.spirit_tree_message_targets_avatar(msg, text, source=source)
        ):
            log.info(f"[{SPIRIT_TREE_AVATAR}] spirit tree sync skipped ({source}): message is not targeted to this avatar.")
            return False

        matched = False
        if indicates_mature:
            needs_harvest = self.record_spirit_tree_mature_state(text, msg=msg, source=source)
            if needs_harvest:
                self.schedule_spirit_tree_harvest_once(source)
            matched = True
        elif indicates_irrigation:
            self.record_spirit_tree_irrigation_state(text, source=source)
            matched = True
        if indicates_invasion:
            needs_guard = self.record_spirit_tree_invasion_state(text, msg=msg, source=source)
            if needs_guard:
                self.schedule_spirit_tree_guard_once(source)
            matched = True
        if not matched:
            self.normalize_spirit_tree_state(SPIRIT_TREE_AVATAR)
        return matched

    def spirit_tree_message_targets_avatar(self, msg, text, source=""):
        """Only accept spirit-tree bot text that can be attributed to 缘生子."""
        avatar = SPIRIT_TREE_AVATAR
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
        if marker:
            return marker.group(1).strip() == avatar
        if msg is None:
            return True

        reply_identity = tracked_command_identity_for_reply(self, msg)
        if reply_identity:
            return reply_identity == avatar

        if mentions_other_user_for_identity(self, msg, text, avatar):
            return False

        lower_text = str(text or "").lower()
        for username, identity in (self.avatar_usernames or {}).items():
            if identity != avatar:
                continue
            name = str(username or "").lower().lstrip("@").strip()
            if not name:
                continue
            if f"@{name}" in lower_text or f"【{name}】" in lower_text:
                return True
            if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lower_text):
                return True

        return False

    async def execute_spirit_tree_harvest_once(self, reason="mature"):
        avatar = SPIRIT_TREE_AVATAR
        await self.startup_done.wait()
        await self.pause_event.wait()
        async with AtomicTaskContext(self, f"SpiritTreeHarvest-{avatar}"):
            a_state = self.normalize_spirit_tree_state(avatar)
            mature_until = a_state.get("spirit_tree_mature_until", "")
            if a_state.get("spirit_tree_status") != SPIRIT_TREE_MATURE_STATUS or not (mature_until and is_future(mature_until)):
                return
            if a_state.get("spirit_tree_harvested_in_mature_period") or a_state.get("spirit_tree_harvest_attempted_in_mature_period"):
                return
            a_state["spirit_tree_harvest_attempted_in_mature_period"] = True
            a_state["spirit_tree_harvest_pending"] = False
            self.save_state()
            log.info(f"[{avatar}] spirit tree mature detected ({reason}); sending {SPIRIT_TREE_HARVEST_COMMAND} once.")
            resp = await self.send_and_wait_feedback_identity(avatar, SPIRIT_TREE_HARVEST_COMMAND, timeout=90, max_retries=1)
            resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            self.record_spirit_tree_harvest_response(resp_text)

    async def execute_spirit_tree_guard_once(self, reason="invasion"):
        avatar = SPIRIT_TREE_AVATAR
        await self.startup_done.wait()
        await self.pause_event.wait()
        async with AtomicTaskContext(self, f"SpiritTreeGuard-{avatar}"):
            a_state = self.get_avatar_state(avatar)
            next_guard = a_state.get("next_spirit_tree_guard_time", "")
            if next_guard and is_future(next_guard):
                a_state["spirit_tree_invasion_status"] = ""
                a_state["spirit_tree_guard_pending"] = False
                self.save_state()
                return
            if not a_state.get("spirit_tree_guard_pending") and a_state.get("spirit_tree_invasion_status") != "古剑门来袭":
                return
            a_state["spirit_tree_guard_pending"] = False
            self.save_state()
            log.info(f"[{avatar}] 古剑门来袭 detected ({reason}); sending {SPIRIT_TREE_GUARD_COMMAND} once.")
            resp = await self.send_and_wait_feedback_identity(avatar, SPIRIT_TREE_GUARD_COMMAND, timeout=90, max_retries=1)
            resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            self.record_spirit_tree_guard_response(resp_text)

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
        recent_identity = recent_profile_identity_for_text(self, text, msg_id=getattr(msg, "id", None))
        if not self.text_targets_self(msg, text) and not recent_identity:
            return

        avatar = recent_identity or None
        attribution_reliable = bool(recent_identity)
        # 优先根据 sender_id 判断发送者（化身有独立 chat_id）
        sender_id = str(getattr(msg, "sender_id", ""))
        if not avatar and sender_id == "-1004240160265":
            avatar = "无咎子"; attribution_reliable = True
        elif not avatar and sender_id == "-1003809391782":
            avatar = "缘生子"; attribution_reliable = True
        elif not avatar and sender_id == "-1003999815554":
            avatar = "素缘子"; attribution_reliable = True

        # 退化到 reply_to 查找
        if not avatar:
            reply_to = getattr(msg, 'reply_to', None)
            if reply_to:
                reply_to_id = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'channel_post', None)
                if reply_to_id and reply_to_id in self.command_avatar_map:
                    avatar = self.command_avatar_map.get(reply_to_id)
                    attribution_reliable = True
        # 退化到文本特征
        if not avatar:
            if "[Avatar: 无咎子]" in text: avatar = "无咎子"; attribution_reliable = True
            elif "[Avatar: 缘生子]" in text: avatar = "缘生子"; attribution_reliable = True
            elif "[Avatar: 素缘子]" in text: avatar = "素缘子"; attribution_reliable = True
            elif "神念重归主魂肉身" in text or "当前操控：主魂" in text: avatar = "主魂"; attribution_reliable = True
        # 最后 fallback: current_identity（不可靠）
        if not avatar:
            avatar = self.current_identity
            attribution_reliable = False

        # 统一解析境界和修为（主魂+化身都更新）
        # ⚠️ 必须有 attribution_reliable 守卫！
        # current_identity fallback 不可靠——主魂/他人发的指令可能污染化身数据
        if attribution_reliable:
            record_cultivation_profile_from_text(
                self, text, identity=avatar, logger=log, source="passive profile"
            )

        now = now_str()

        # ---- 闭关相关（主魂+化身） ----
        # 强行出关 / 出关成功 → 清除深度闭关状态
        # "功成圆满" 会出现在 ".查看闭关" 正常回复中（"即可功成圆满"），必须排除仍在闭关的情况。
        _is_real_exit = any(k in text for k in ["强行出关", "出关成功", "已出关", "闭关结束"]) or (
            "功成圆满" in text and "预计还需" not in text and "还需" not in text and "正在" not in text
        )
        if _is_real_exit:
            if avatar == "主魂":
                self.state["in_deep_meditation"] = False
                self.state["deep_meditation_end_time"] = ""
                self.state["is_closing"] = False
                self.meditation_state_event.set()
            else:
                self.set_avatar_state(avatar, "in_deep_meditation", False)
                self.set_avatar_state(avatar, "deep_meditation_end_time", "")
            log.info(f"[{avatar}] passive: closing state cleared (出关).")

        # 深度闭关中 / 预计还需 → 更新深度闭关结束时间
        elif any(k in text for k in ["深度闭关", "预计还需", "闭关修炼"]):
            is_ongoing = any(k in text for k in ["预计还需", "还需"])
            if any(k in text for k in ["未处于深度闭关", "并未处于深度闭关", "结算", "归位"]) or ("功成圆满" in text and not is_ongoing):
                if avatar == "主魂":
                    self.state["in_deep_meditation"] = False
                    self.state["deep_meditation_end_time"] = ""
                    self.meditation_state_event.set()
                else:
                    self.set_avatar_state(avatar, "in_deep_meditation", False)
                    self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                log.info(f"[{avatar}] passive: deep meditation ended.")
            else:
                cd = self.parse_wait_time(text)
                if cd > 0:
                    if avatar == "主魂":
                        self.state["in_deep_meditation"] = True
                        self.state["deep_meditation_end_time"] = add_seconds_str(now, cd)
                        self.meditation_state_event.set()
                    else:
                        self.set_avatar_state(avatar, "in_deep_meditation", True)
                        self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now, cd))
                    log.info(f"[{avatar}] passive: deep meditation active, {cd}s remaining.")

        # 闭关冷却中 → 更新 next_meditation_time
        if "闭关冷却" in text or "闭关剩余" in text:
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
        for key, value in state.items():
            if key == "next_concubine_voyage_time" and not self.concubine_voyage_enabled(identity):
                continue
            if key in ignored_keys or not isinstance(value, str) or not value:
                continue
            if not (
                (key.startswith("next_") and key.endswith("_time"))
                or key in watch_keys
            ):
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
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()

        yield_attempts = 0
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            async with self.avatar_send_lock:
                if self.current_identity != identity:
                    wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                    if 0 <= wait_sec <= 60:
                        if yield_attempts == 0 or yield_attempts % 12 == 0:
                            log.info(
                                f"Avatar switch deferred: {self.current_identity} has commands due "
                                f"in {wait_sec:.1f}s. [{identity}] waits."
                            )
                        yield_attempts += 1
                        should_yield = True
                        wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))

                    if not should_yield:
                        switch_cmd = f".切换 {identity}" if identity != "主魂" else ".切换 主魂"
                        log.info(f"Avatar switch: {self.current_identity} -> {identity}")
                        switch_resp = await self._send_and_wait_feedback_raw(switch_cmd, timeout=30, max_retries=2)
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        if not resp_str or not any(k in resp_str for k in ["成功", "已切换", "当前操控", identity]):
                            log.error(f"Avatar switch to {identity} FAILED!")
                            return None
                        self.current_identity = identity
                        self._main_confirmed = (identity == "主魂")
                        await asyncio.sleep(2)
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
        if self._avatar_loop_active or self._avatar_loop_count > 0:
            return
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
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
            return False
        cd = self.parse_wait_time(response_text)
        if cd <= 0:
            verify_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
            verify_text = getattr(verify_resp, "text", "") if hasattr(verify_resp, "text") else verify_resp if isinstance(verify_resp, str) else str(verify_resp) if verify_resp else ""
            verify_cd = self.parse_wait_time(verify_text)
            if verify_cd > 0 and is_deep_meditation_ongoing_response(verify_text):
                cd = verify_cd
        self.set_avatar_state(avatar, "in_deep_meditation", True)
        self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600))
        return True

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
            await self.send_and_wait_feedback_identity(avatar, deep_cmd, timeout=60, return_response_msg=False)
            self.set_avatar_state(avatar, "in_deep_meditation", True)
            return False, resp_text
        return True, resp_text

    # ============================================================
    # 身外化身：共历心劫
    # ============================================================

    async def execute_avatar_heart_trial(self, avatar, status_msg):
        async with AtomicTaskContext(self, f"HeartTrial-{avatar}"):
            status_text = getattr(status_msg, "text", "") if hasattr(status_msg, "text") else ""
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
                        schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                        log.info(f"🟢 OUT [{avatar}]:\n.稳 ({round_num}/3, try {attempt}/3)")
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] heart trial: failed to send .稳 ({round_num}/3): {e}")
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
        while self.is_running:
            await self.pause_event.wait()
            await self.startup_done.wait()
            today = datetime.now().strftime("%Y-%m-%d")
            a_state = self.get_avatar_state(avatar)
            if a_state.get("last_tower_date") == today:
                await asyncio.sleep(max(60, ((datetime.now()+timedelta(days=1)).replace(hour=0,minute=0,second=0)-datetime.now()).total_seconds()))
                continue
            now = datetime.now()
            if now.hour < 23:
                await asyncio.sleep(max(60, (now.replace(hour=23,minute=0,second=0)-now).total_seconds()))
                continue
            if now.hour == 23 and now.minute < 30:
                await asyncio.sleep(random.randint(0,1800))
            resp = await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=120)
            if "修为不足" in (getattr(resp,"text","") if hasattr(resp,"text") else ""):
                async def rt(): return await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=120)
                await self.handle_修为不足(avatar, rt, cooldown_key="last_tower_date")
            self.set_avatar_state(avatar, "last_tower_date", today)
            await asyncio.sleep(3600)

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
                        # 0. 每日点卯
                        if features.get("daily_checkin"): await self._avatar_daily_checkin(avatar)
                        # 1. 闭关
                        await self._avatar_meditation_check(avatar)
                        # 2. 野外历练
                        await self._avatar_field_training_check(avatar)
                        # 3. 阵法 (星宫)
                        if features.get("formation"): await self.execute_avatar_formation(avatar)
                        elif features.get("formation_assist") and self.pending_formation_invite_msg:
                            await self._avatar_assist_formation(avatar)
                        # 4. 闯塔
                        if features.get("tower"): await self._avatar_tower_check(avatar)
                        # 5. 灵树灌溉
                        if features.get("spirit_tree_irrigation"): await self._avatar_spirit_tree_irrigation_check(avatar)
                        # 5. 入梦寻图 / 星宫道心侍妾远航绑定批次
                        if features.get("dream_map"):
                            handled_bound_batch = await self.execute_avatar_bound_dream_voyage(avatar)
                            if not handled_bound_batch:
                                await self._avatar_dream_map_check(avatar)
                                await self.execute_avatar_concubine_voyage(avatar)
                        # 7. 共历心劫
                        if features.get("heart_trial"): await self._avatar_heart_trial_check(avatar)
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] error: {e}")
                    log.info(f"Avatar [{avatar}] cycle complete")
            finally:
                self._avatar_loop_active = False  # 所有化身完成后才释放
            # 按最短CD等待，而非固定30分钟
            cd = await self._get_avatar_min_cd_seconds()
            log.info(f"All avatars done. Next cycle in {cd}s.")
            await asyncio.sleep(cd)

    async def _avatar_daily_checkin(self, avatar):
        today = datetime.now().strftime("%Y-%m-%d")
        if seconds_until_daily_task_start(datetime.now()) > 0:
            return
        if self.get_avatar_state(avatar).get("last_dianmao_date") == today:
            return
        resp = await self.send_and_wait_feedback_identity(avatar, ".宗门点卯", timeout=60)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
        if resp_text:
            self.set_avatar_state(avatar, "last_dianmao_date", today)

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

        # 内存状态快速路径：如果标记为深度闭关且未到期，直接跳过（不阻塞）
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            if end_time and is_future(end_time):
                log.info(f"[{avatar}] 深度闭关中，剩余 {seconds_until(end_time)}s，跳过闭关检查。")
                return  # 不 sleep，让外层循环继续检查其他任务（历练/心劫/入梦等）
            self.set_avatar_state(avatar, "in_deep_meditation", False)
            self.set_avatar_state(avatar, "deep_meditation_end_time", "")

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
                    self.set_avatar_state(avatar, "in_deep_meditation", True)
                    self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now_str(), cd))
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
                if any(k in cr_text for k in ["冷却", "无法立即"]) or ("需要" in cr_text and "分钟" in cr_text):
                    cd = self.parse_wait_time(cr_text)
                    log.info(f"[{avatar}] 闭关冷却中 {cd}s，跳过。")
                    return

            await asyncio.sleep(3)

            # Step 4: .深度闭关
            log.info(f"[{avatar}] 发送 .深度闭关")
            dr = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
            dr_text = getattr(dr, "text", "") if hasattr(dr, "text") else str(dr) if dr else ""
            await self.record_avatar_deep_meditation_start(avatar, dr_text)

    async def _avatar_tower_check(self, avatar):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.get_avatar_state(avatar).get("last_tower_date") == today: return
        if datetime.now().hour < 23: return
        resp = await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=120)
        if "修为不足" in (getattr(resp,"text","") if hasattr(resp,"text") else ""):
            async def rt(): return await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=120)
            await self.handle_修为不足(avatar, rt, cooldown_key="last_tower_date")
        self.set_avatar_state(avatar, "last_tower_date", today)

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

    async def _avatar_spirit_tree_irrigation_check(self, avatar):
        """化身灵树灌溉检查（每2小时一次）。"""
        a_state = self.normalize_spirit_tree_state(avatar)
        if a_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
            mature_until = a_state.get("spirit_tree_mature_until", "")
            if mature_until and is_future(mature_until):
                if not (
                    a_state.get("spirit_tree_harvested_in_mature_period")
                    or a_state.get("spirit_tree_harvest_attempted_in_mature_period")
                ):
                    self.schedule_spirit_tree_harvest_once("irrigation check")
                log.info(f"[{avatar}] 灵树处于成熟采摘期，暂停灌溉至 {mature_until}。")
                return
        nt = a_state.get("next_spirit_tree_irrigation_time", "")
        if nt and is_future(nt): return
        resp = await self.send_and_wait_feedback_identity(avatar, SPIRIT_TREE_IRRIGATION_COMMAND, timeout=60)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else ""
        if self.maybe_record_spirit_tree_passive_message(None, resp_text, source="irrigation response"):
            return
        a_state = self.get_avatar_state(avatar)
        a_state["spirit_tree_status"] = SPIRIT_TREE_IRRIGATION_STATUS
        # 处理冷却时间（从响应中解析或使用默认值）
        cd = self.parse_wait_time(resp_text)
        a_state["next_spirit_tree_irrigation_time"] = add_seconds_str(now_str(), cd if cd > 0 else 2*3600)
        self.save_state()


    async def _avatar_field_training_check(self, avatar):
        """化身野外历练检查（单次）。无咎子先发 .推命 探索，再发 .野外历练 深入"""
        a_state = self.get_avatar_state(avatar)
        nt = a_state.get("next_field_training_time", "")
        if nt and is_future(nt): return
        features = self.avatar_features.get(avatar, {})
        prefix = features.get("meditation_prefix", "")
        training_cmd = features.get("training_cmd", ".野外历练")
        training_level = features.get("training_level", "谨慎")
        # 推命前缀：先发 ".推命 探索"，再发 ".野外历练 深入"
        if prefix:
            await self.send_and_wait_feedback_identity(avatar, f"{prefix} 探索")
            await asyncio.sleep(3)
        resp = await self.send_and_wait_feedback_identity(avatar, f"{training_cmd} {training_level}", timeout=90)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else ""
        if "修为不足" in resp_text:
            async def rt(): return await self.send_and_wait_feedback_identity(avatar, f"{training_cmd} {training_level}", timeout=90)
            success, _ = await self.handle_修为不足(avatar, rt, cooldown_key="next_field_training_time")
            if not success: return
        cd = self.parse_wait_time(resp_text)
        self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now_str(), cd if cd > 0 else 4*3600))

    async def _get_avatar_min_cd_seconds(self):
        """计算所有化身中最早到期的CD时间（秒），用于替代固定30分钟sleep"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        min_cd = 1800  # 兜底30分钟
        for avatar in self.avatars:
            a_state = self.get_avatar_state(avatar)
            features = self.avatar_features.get(avatar, {})
            if features.get("daily_checkin") and a_state.get("last_dianmao_date") != today and seconds_until_daily_task_start(now) <= 0:
                min_cd = min(min_cd, 60)
            # 闭关CD
            if a_state.get("in_deep_meditation"):
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
            # 入梦/远航绑定CD
            if features.get("dream_map"):
                if self.concubine_voyage_enabled(avatar) and not self.dashboard_command_paused(".侍妾远航 均衡", avatar):
                    bound_time = self.latest_concubine_dream_voyage_time(avatar)
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
                and not self.dashboard_command_paused(".侍妾远航 均衡", avatar)
            ):
                voyage = a_state.get("next_concubine_voyage_time", "")
                if voyage and is_future(voyage):
                    min_cd = min(min_cd, seconds_until(voyage))
            # 灵树灌溉CD
            if features.get("spirit_tree_irrigation"):
                if a_state.get("spirit_tree_status") == SPIRIT_TREE_MATURE_STATUS:
                    mature_until = a_state.get("spirit_tree_mature_until", "")
                    if mature_until and is_future(mature_until):
                        min_cd = min(min_cd, seconds_until(mature_until))
                        continue
                st = a_state.get("next_spirit_tree_irrigation_time", "")
                if st and is_future(st):
                    min_cd = min(min_cd, seconds_until(st))
        return max(min_cd, 60)  # 至少等60秒
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

                # 1) 天阶状态同步
                # 只在缓存缺失时补账；过期代表可以直接登阶，不再先查状态。
                next_stairs = self.restore_cloud_stairs_time_from_last()
                if self.state.get("cloud_stairs_progress") and next_stairs:
                    log.info(f"Startup Sync: Cloud stairs cache present. Skipping .天阶状态. Next: {next_stairs}")
                else:
                    log.info("Startup Sync: Cloud stairs cache missing. Querying .天阶状态...")
                    resp = await self.send_and_wait_feedback(".天阶状态", force_identity_check=True)
                    if resp:
                        # 尝试抓取进度
                        self.update_cloud_stairs_progress_from_text(resp, source="Startup Sync cloud stairs")

                        # 登天阶 CD 解析：只看登阶冷却行，避免误抓罡风 CD
                        cd = self.parse_wait_time(resp, line_identifier="登阶冷却")
                        if cd > 0:
                            # last_stairs_time 设为 "现在 - (CD - 30min)"，估计上次登阶时间
                            self.state["last_stairs_time"] = add_seconds_str(now_str(), cd - 1800)
                            self.state["next_stairs_time"] = add_seconds_str(now_str(), cd)
                        elif "可立即登阶" in resp:
                            self.state["last_stairs_time"] = now_str()
                            self.state["next_stairs_time"] = ""

                        # 罡风 CD 解析
                        wind_cd = self.parse_wait_time(resp, line_identifier="引九天罡风")
                        if wind_cd > 0:
                            self.state["nine_heaven_wind_cd_time"] = add_seconds_str(now_str(), wind_cd)
                        elif "可立即施展" in resp:
                            self.state["nine_heaven_wind_cd_time"] = 0

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
                            self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd_med)
                            self.state["in_deep_meditation"] = True
                            log.info(f"Startup Sync: Meditation end time found: {self.state['deep_meditation_end_time']}")
                        elif is_deep_meditation_settlement_response(resp_med) or is_not_deep_meditation_response(resp_med):
                            self.state["in_deep_meditation"] = False
                            self.state["deep_meditation_end_time"] = ""
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

        # ---- 启动所有独立循环 ----
        # 每个循环都是独立的 asyncio.Task，通过 startup_done.wait() 等待同步完成

        # 每日任务循环（闯塔、点卯、传功）
        asyncio.create_task(self.run_daily_tasks())

        # 云阶问心循环（主循环）
        asyncio.create_task(self.run_cloud_stairs_loop())

        # 九天罡风循环（独立于登天阶循环）
        asyncio.create_task(self.run_nine_heaven_wind_loop())

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

        # 通用固定冷却指令循环（继承自 CommonCommandMixin）
        asyncio.create_task(self.run_field_training_loop())
        asyncio.create_task(self.run_sect_war_loop())
        asyncio.create_task(self.run_custom_command_loop())

        # ---- 化身系统 ----
        asyncio.create_task(self.run_all_avatars_sequential())
        asyncio.create_task(self.run_star_gazing_loop())


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
                    # 编辑后出现元婴遁逃·虚弱 → 立刻告警并停止脚本（防漏检补丁）
                    if self.is_rift_weakness_response(text) and is_edited_message_for_current_account(self, msg, text):
                        log.critical(f"Rift weakness DETECTED in edited message! Stopping immediately.\n{text}")
                        await self.stop_for_rift_weakness(text)
                        return
                    manual_reply = is_reply_to_manual_command(self, msg)
                    manual_processed = await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                    if not manual_reply or not manual_processed:
                        self.maybe_record_spirit_tree_passive_message(msg, text, source="edited message")
                    # 编辑消息也能触发 feedback_events（bot 通过编辑回复指令）
                    is_matched = False
                    if msg.reply_to:
                        replied_id = getattr(msg.reply_to, 'reply_to_msg_id', None)
                        if replied_id and replied_id in self.feedback_events:
                            evt = self.feedback_events[replied_id]
                            if not evt.is_set():
                                cmd_text = self.feedback_commands.get(replied_id, "")
                                cmd_identity = getattr(self, "feedback_identities", {}).get(replied_id, getattr(self, "current_identity", "主魂"))
                                _skip_reply = False
                                if mentions_other_user_for_identity(self, msg, text, cmd_identity):
                                    log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: targets another user (msg {msg.id}): {text[:80]}")
                                    _skip_reply = True
                                if feedback_response_conflicts(cmd_text, text) and not feedback_response_matches_command(cmd_text, text):
                                    log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: response family conflict (msg {msg.id}): {text[:80]}")
                                    _skip_reply = True
                                if (
                                    not _skip_reply
                                    and feedback_response_requires_positive_match(cmd_text)
                                    and not feedback_response_matches_command(cmd_text, text)
                                ):
                                    log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                                    _skip_reply = True
                                if cmd_text == ".查看闭关" and not self.is_loose_feedback_candidate(cmd_text, text):
                                    log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                                    _skip_reply = True
                                if not _skip_reply:
                                    self.last_feedback_text[replied_id] = text
                                    self.last_feedback_msg[replied_id] = msg
                                    evt.set()
                                    is_matched = True
                                    log.info(f"[EDITED-FEEDBACK] Matched by reply_to={replied_id}")
                    if not is_matched and msg.id in self.feedback_events:
                        evt = self.feedback_events[msg.id]
                        if not evt.is_set():
                            cmd_text = self.feedback_commands.get(msg.id, "")
                            cmd_identity = getattr(self, "feedback_identities", {}).get(msg.id, getattr(self, "current_identity", "主魂"))
                            _skip_reply = False
                            if mentions_other_user_for_identity(self, msg, text, cmd_identity):
                                log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: targets another user (msg {msg.id}): {text[:80]}")
                                _skip_reply = True
                            if feedback_response_conflicts(cmd_text, text) and not feedback_response_matches_command(cmd_text, text):
                                log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: response family conflict (msg {msg.id}): {text[:80]}")
                                _skip_reply = True
                            if (
                                not _skip_reply
                                and feedback_response_requires_positive_match(cmd_text)
                                and not feedback_response_matches_command(cmd_text, text)
                            ):
                                log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                                _skip_reply = True
                            if not _skip_reply:
                                self.last_feedback_text[msg.id] = text
                                self.last_feedback_msg[msg.id] = msg
                                evt.set()
                                is_matched = True
                                log.info(f"[EDITED-FEEDBACK] Matched by msg_id={msg.id}")
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
            """
            检测当前是否处于"活跃的观星窗口"内。
            观星窗口 = 目标显化时间 - STAR_GAZING_MONITOR_LEAD_SECONDS(3分钟)
            如果当前时间在窗口内且尚未到改换星移发送时间，返回目标显化时间。

            用于 handle_game_response 中快速判断是否该处理显化事件。
            """
            now = now or datetime.now()
            for target in (
                now.replace(minute=0, second=0, microsecond=0),
                self.next_star_manifest_dt(now),
            ):
                if target.hour % STAR_GAZING_INTERVAL_HOURS != 0:
                    continue
                window_start = target - timedelta(seconds=STAR_GAZING_MONITOR_LEAD_SECONDS)
                shift_time = target - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)
                if window_start <= now <= shift_time:
                    return target
            return None

    def next_star_gazing_window_start_dt(self, now=None):
            """
            计算下一个观星窗口的起始时间和对应的目标显化时间。
            返回 (window_start, target_dt) 元组。
            如果当前已过窗口截止时间，自动跳到下一轮。
            """
            now = now or datetime.now()
            target = self.next_star_manifest_dt(now)
            window_start = target - timedelta(seconds=STAR_GAZING_MONITOR_LEAD_SECONDS)
            if now > target - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS):
                # 当前窗口已过截止时间，跳到下一轮
                target += timedelta(hours=STAR_GAZING_INTERVAL_HOURS)
                window_start = target - timedelta(seconds=STAR_GAZING_MONITOR_LEAD_SECONDS)
            return window_start, target

    def star_observatory_needs_calm(self, text):
            """检测观星台状态是否需要安抚（星辰流紊乱或黯淡）。"""
            return bool(text and ("紊乱" in text or "黯淡" in text))

    def star_gazing_sent_on_date(self, date_str=None):
            """检查指定日期是否已经发送过 .观星。"""
            date_str = date_str or datetime.now().strftime("%Y-%m-%d")
            return self.state.get("last_gazing_date") == date_str

    def star_gazing_send_dt(self, target_dt):
            """
            计算发送 .观星 的时间：在星盘显现前 STAR_GAZING_COMMAND_LEAD_SECONDS(30秒) 发送。
            这样观星结果出来后，刚好赶上显现时间点。
            """
            return target_dt - timedelta(seconds=STAR_GAZING_COMMAND_LEAD_SECONDS)

    def star_gazing_target_for_opportunity(self, now=None):
            """
            计算当前观星机会对应的目标显化时间。
            如果当前已过改换星移发送时间，则跳到下一轮。
            """
            now = now or datetime.now()
            target_dt = self.next_star_manifest_dt(now)
            shift_dt = target_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)
            if now >= shift_dt:
                target_dt += timedelta(hours=STAR_GAZING_INTERVAL_HOURS)
            return target_dt

    def daily_star_gazing_fallback_dt(self, now=None):
            """
            计算每日备用观星时间（23:59）。
            如果当天一整天都没有触发观星，在 23:59 发送一次兜底。
            """
            now = now or datetime.now()
            return now.replace(
                hour=STAR_GAZING_DAILY_FALLBACK_HOUR,
                minute=STAR_GAZING_DAILY_FALLBACK_MINUTE,
                second=0,
                microsecond=0,
            )

    def has_pending_star_gazing_action(self):
            """检查是否有排期中的观星或改换星移操作（任何一项在未来有效）。"""
            pending_gazing_target = self.state.get("pending_star_gazing_target_time", "")
            pending_shift_target = self.state.get("pending_star_shift_target_time", "")
            return bool(
                (pending_gazing_target and is_future(pending_gazing_target))
                or (pending_shift_target and is_future(pending_shift_target))
            )

    def clear_pending_star_gazing_schedule(self):
            """清除所有排期中的观星数据。"""
            self.state["pending_star_gazing_date"] = ""
            self.state["pending_star_gazing_target_time"] = ""
            self.state["pending_star_gazing_scheduled_time"] = ""
            self.state["pending_star_gazing_manifest_time"] = ""

    def clear_star_gazing_round_claim(self):
            """清除账号级观星轮次占用。"""
            self.state["star_gazing_claimed_manifest_time"] = ""
            self.state["star_gazing_claimed_avatar"] = ""
            self.state["pending_star_gazing_manifest_time"] = ""

    def star_gazing_claim_matches(self, avatar, manifest_dt):
            """确认当前任务仍是本账号在该显化轮次被指派的唯一身份。"""
            if not manifest_dt:
                return True
            manifest_key = dt_to_str(manifest_dt)
            expected_avatar = avatar or "主魂"
            return (
                self.state.get("star_gazing_claimed_manifest_time", "") == manifest_key
                and self.state.get("star_gazing_claimed_avatar", "") == expected_avatar
            )

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
            """
            计算下一个星盘显现的整点时间。
            星盘每 3 小时显现一次（0:00, 3:00, 6:00, ...）。
            如果当前时间恰好是整点，返回下一个 3 的倍数整点。

            例如：
                当前 14:20 -> 当前窗口 12:00（已过）-> 下一个 15:00
                当前 14:35 -> 下一个 15:00
            """
            now = now or datetime.now()
            base_hour = (now.hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
            candidate = now.replace(hour=base_hour, minute=0, second=0, microsecond=0)
            if now >= candidate:
                candidate += timedelta(hours=STAR_GAZING_INTERVAL_HOURS)
            return candidate

    def star_shift_done_today(self, today=None):
            """检查今天是否已经执行过改换星移。"""
            today = today or datetime.now().strftime("%Y-%m-%d")
            return self.state.get("last_star_shift_date") == today

    def pending_daily_star_gazing_fallback_dt(self, now=None):
            """
            判断是否需要执行每日备用观星（23:59 兜底）。
            只有在以下所有条件满足时才需要：
              - 今天还没观星
              - 今天还没触发过备用观星
              - 今天还没执行改换星移
              - 没有排期中的操作
              - 当前时间未过 23:59 + 1分钟（即 00:00 之后不再触发）
            """
            now = now or datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self.star_gazing_sent_on_date(today):
                return None
            if self.state.get("last_star_gazing_fallback_date") == today:
                return None
            if self.star_shift_done_today(today):
                return None
            if self.has_pending_star_gazing_action():
                return None
            fallback_dt = self.daily_star_gazing_fallback_dt(now)
            if now >= fallback_dt + timedelta(minutes=1):
                return None
            return fallback_dt

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
                        if not self.star_shift_done_today(target_day):
                            self.state["pending_star_shift_date"] = target_day
                            self.state["pending_star_shift_target_time"] = dt_to_str(target_dt)
                            self.state["pending_star_shift_msg_id"] = resp_msg.id
                            self.save_state()
                            gazing_date = today
                            log.info(
                                f"Star gazing fallback: GOOD result detected; "
                                f"scheduling .改换星移 before {dt_to_str(target_dt)}."
                            )
                            self.star_shift_task = asyncio.create_task(
                                self.schedule_star_shift(resp_msg.id, target_dt, gazing_date)
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
            """
            计算下一个星盘显现的整点时间。
            星盘每 3 小时显现一次（0:00, 3:00, 6:00, ...）。
            如果当前时间恰好是整点，返回下一个 3 的倍数整点。

            例如：
                当前 14:20 -> 当前窗口 12:00（已过）-> 下一个 15:00
                当前 14:35 -> 下一个 15:00
            """
            now = now or datetime.now()
            base_hour = (now.hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
            candidate = now.replace(hour=base_hour, minute=0, second=0, microsecond=0)
            if now >= candidate:
                candidate += timedelta(hours=STAR_GAZING_INTERVAL_HOURS)
            return candidate

    def star_shift_done_today(self, today=None):
            """检查今天是否已经执行过改换星移。"""
            today = today or datetime.now().strftime("%Y-%m-%d")
            return self.state.get("last_star_shift_date") == today

    def pending_daily_star_gazing_fallback_dt(self, now=None):
            """
            判断是否需要执行每日备用观星（23:59 兜底）。
            只有在以下所有条件满足时才需要：
              - 今天还没观星
              - 今天还没触发过备用观星
              - 今天还没执行改换星移
              - 没有排期中的操作
              - 当前时间未过 23:59 + 1分钟（即 00:00 之后不再触发）
            """
            now = now or datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self.star_gazing_sent_on_date(today):
                return None
            if self.state.get("last_star_gazing_fallback_date") == today:
                return None
            if self.star_shift_done_today(today):
                return None
            if self.has_pending_star_gazing_action():
                return None
            fallback_dt = self.daily_star_gazing_fallback_dt(now)
            if now >= fallback_dt + timedelta(minutes=1):
                return None
            return fallback_dt
    def star_gazing_good_opportunity(self, text):
        return bool(text and any(keyword in text for keyword in STAR_GAZING_GOOD_KEYWORDS))


    async def schedule_star_shift(self, reply_msg_id, target_dt, gazing_date=None):
            """
            调度 .改换星移 指令的发送。
            核心策略：在星盘显现后 25 秒发送，给机器人拥堵队列留出最终快报前的处理时间。

            流程:
              1. 计算发送时间 = target_dt - (-25秒) = target_dt + 25秒
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
                # 显现后 25 秒发送（STAR_GAZING_SHIFT_LEAD_SECONDS = -25）
                shift_dt = target_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)
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

                # 如果当前时间已超过最后一次发送时间，说明错过了窗口
                if datetime.now() > last_send_dt + timedelta(seconds=1):
                    log.warning(
                        f"Star gazing: missed shift repeat window for {dt_to_str(target_dt)}; "
                        f"clearing pending shift."
                    )
                    self.clear_pending_star_shift()
                    self.save_state()
                    return

                command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
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
                    if datetime.now() > send_dt + timedelta(seconds=2):
                        log.warning(
                            f"Star gazing: skipped expired shift repeat "
                            f"{idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} scheduled at {dt_to_str(send_dt)}."
                        )
                        continue
                    log.info(
                        f"Star gazing: sending {command} repeat {idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} "
                        f"as reply to .观星 result {reply_msg_id}."
                    )
                    sent_msg = await self.send_and_wait_feedback_identity("主魂", command, timeout=10, reply_to=reply_msg_id)
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
            # 改换星移在显现后 25 秒发送（STAR_GAZING_SHIFT_LEAD_SECONDS = -25）
            shift_dt = target_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)
            if self.get_avatar_state(avatar).get("last_star_shift_date") == today:
                return
            if datetime.now() > shift_dt + timedelta(seconds=5):
                return

            wait_sec = (shift_dt - datetime.now()).total_seconds()
            if wait_sec > 0:
                log.info(f"Star gazing [{avatar}]: waiting {int(wait_sec)}s for .改换星移 at {dt_to_str(shift_dt)}.")
                await asyncio.sleep(wait_sec)

            if self.get_avatar_state(avatar).get("last_star_shift_date") == today:
                return

            self.active_atomic_task = asyncio.current_task()
            log.info(f"🔒 [ATOMIC LOCK] Acquired by AvatarStarShift-{avatar}")
            try:
                command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
                log.info(f"Star gazing [{avatar}]: sending {command} as reply to .观星 result {reply_msg_id}.")
                sent_msg = await self.send_and_wait_feedback_identity(avatar, command, reply_to=reply_msg_id)
                if sent_msg:
                    self.set_avatar_state(avatar, "last_star_shift_date", today)
                    self.set_avatar_state(avatar, "last_star_shift_time", now_str())
                    log.info(f"Star gazing [{avatar}]: .改换星移 sent successfully.")
                else:
                    log.warning(f"Star gazing [{avatar}]: .改换星移 send FAILED.")
            finally:
                if self.active_atomic_task == asyncio.current_task():
                    self.active_atomic_task = None
                    log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarShift-{avatar}")

    async def schedule_star_gazing_simple(self, send_dt, immediate_shift=False, avatar=None, manifest_dt=None):
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
                await asyncio.sleep(wait_sec)

            today = datetime.now().strftime("%Y-%m-%d")
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
                    )

                if not resp_msg:
                    if avatar:
                        log.warning(f"Star gazing [{avatar}]: .观星 failed completely (no response).")
                    else:
                        log.warning("Star gazing: .观星 failed completely (no response). Will retry later or fallback at 23:59.")
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
                else:
                    self.state["last_gazing_date"] = today
                    self.state["last_gazing_time"] = now_str()
                    self.clear_pending_star_gazing_schedule()
                    self.save_state()

                # 如果结果是 Good，触发改换星移
                resp_text = (resp_msg.text or "")
                if self.star_gazing_good_opportunity(resp_text):
                    if immediate_shift:
                        # ---- 当前窗口活跃：计算发送 .改换星移 的准确时间 ----
                        command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
                        current_manifest_hour = (datetime.now().hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
                        current_manifest_dt = datetime.now().replace(
                            hour=current_manifest_hour, minute=0, second=0, microsecond=0
                        )
                        shift_dt = current_manifest_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)
                    
                        now2 = datetime.now()
                        if now2 < shift_dt:
                            wait_sec = (shift_dt - now2).total_seconds()
                            who = avatar or "主魂"
                            log.info(
                                f"Star gazing [{who}]: GOOD result during ACTIVE window, "
                                f"but too early for shift. Waiting {wait_sec:.1f}s until {dt_to_str(shift_dt)}."
                            )
                            await asyncio.sleep(wait_sec)
                    
                        who = avatar or "主魂"
                        log.info(
                            f"Star gazing [{who}]: sending {command} in ACTIVE window as reply to msg {resp_msg.id}."
                        )
                        if avatar:
                            sent_msg = await self.send_and_wait_feedback_identity(avatar, command, reply_to=resp_msg.id)
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
                            log.warning(f"Star gazing [{who}]: .改换星移 send FAILED in immediate mode.")
                        if not avatar:
                            self.clear_pending_star_shift()
                            self.save_state()
                    else:
                        # ---- 常规模式：排期到下一个显化窗口 ----
                        target_dt = self.next_star_manifest_dt(datetime.now())
                        target_day = target_dt.strftime("%Y-%m-%d")
                        if avatar:
                            if self.get_avatar_state(avatar).get("last_star_shift_date") != target_day:
                                log.info(
                                    f"Star gazing [{avatar}]: GOOD result; scheduling .改换星移 before {dt_to_str(target_dt)}."
                                )
                                asyncio.create_task(
                                    self.avatar_schedule_star_shift(avatar, resp_msg.id, target_dt, today)
                                )
                        else:
                            if not self.star_shift_done_today(target_day):
                                self.state["pending_star_shift_date"] = target_day
                                self.state["pending_star_shift_target_time"] = dt_to_str(target_dt)
                                self.state["pending_star_shift_msg_id"] = resp_msg.id
                                self.save_state()
                                log.info(
                                    f"Star gazing: GOOD result; scheduling .改换星移 before {dt_to_str(target_dt)}."
                                )
                                self.star_shift_task = asyncio.create_task(
                                    self.schedule_star_shift(resp_msg.id, target_dt, today)
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
            3. 凌晨 1:00 之前忽略，避免前一天延迟消息误触发。

            返回:
                True 表示消息已被处理（是 Good 显化类事件），False 表示不是。
            """
            # 检查是否为我们关注的 Good 关键词（用于改换星移）
            is_our_good = self.star_gazing_good_opportunity(text)
            # 检查是否为任意 Good 级别事件（包括不在目标列表中的）
            is_any_good = bool(text and "【Good -" in text)

            if not is_any_good:
                return False
            if not sender or not is_game_bot_sender(self, sender):
                return False

            now = datetime.now()
            # 凌晨 1:00 之前忽略，避免昨天延迟消息误触发
            if now.hour < STAR_GAZING_OPPORTUNITY_START_HOUR:
                log.info(
                    f"Star gazing: ignoring manifest message before "
                    f"{STAR_GAZING_OPPORTUNITY_START_HOUR}:00 (likely stale)."
                )
                return False

            today = now.strftime("%Y-%m-%d")
            if self.star_gazing_sent_on_date(today):
                return True  # 今天已经观星了，但仍算事件已处理

            # 构造发送者信息用于日志
            sender_info = (
                f"@{sender.username}"
                if sender and sender.username
                else f"id={msg.sender_id}"
            )
            text_preview = (text[:150] + "...") if len(text) > 150 else text

            if is_our_good:
                # 判断当前显化窗口是否仍然活跃（5分钟改换星移时间内）
                current_manifest_hour = (now.hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
                current_manifest_dt = now.replace(
                    hour=current_manifest_hour, minute=0, second=0, microsecond=0
                )
                window_active_until = current_manifest_dt + timedelta(
                    seconds=STAR_GAZING_ACTIVE_WINDOW_SECONDS
                )

                if now <= window_active_until:
                    # 当前窗口仍然活跃 → 立即发送 .观星 抢占窗口
                    manifest_dt = current_manifest_dt
                    send_dt = now + timedelta(seconds=3)
                    immediate_shift = True
                else:
                    # 当前窗口已过 → 排期到下一个窗口前 1 分钟
                    manifest_dt = self.next_star_manifest_dt(now)
                    send_dt = manifest_dt - timedelta(minutes=1)
                    immediate_shift = False

                async with self.star_gazing_lock:
                    manifest_key = dt_to_str(manifest_dt)
                    claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                    claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                    if claimed_manifest == manifest_key and claimed_avatar:
                        log.info(
                            f"Star gazing: manifest {manifest_key} already assigned to {claimed_avatar}; "
                            f"skip duplicate trigger from {sender_info}: {text_preview}"
                        )
                        return True

                    selected_avatar, idx = self.choose_star_gazing_avatar_for_today(today)
                    if not selected_avatar:
                        log.info("观星轮换: 今天所有化身都已观星，跳过本轮显化。")
                        return True

                    # 清除旧的排期，并安全取消已有后台任务，防止并发多次发送
                    self.clear_pending_star_gazing_schedule()
                    if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
                        self.star_gazing_task.cancel()
                        log.info("Star gazing: cancelled previous pending task to avoid concurrent runs.")

                    self.state["pending_star_gazing_date"] = today
                    self.state["pending_star_gazing_target_time"] = dt_to_str(send_dt)
                    self.state["pending_star_gazing_scheduled_time"] = dt_to_str(send_dt)
                    self.state["pending_star_gazing_manifest_time"] = manifest_key
                    self.state["star_gazing_claimed_manifest_time"] = manifest_key
                    self.state["star_gazing_claimed_avatar"] = selected_avatar
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
                    gazing_date = self.state.get("last_gazing_date") or pending_date
                    self.star_shift_task = asyncio.create_task(
                        self.schedule_star_shift(pending_msg_id, target_dt, gazing_date)
                    )
                else:
                    log.info(
                        f"Star gazing: clearing expired pending shift for {pending_target}."
                    )
                    self.clear_pending_star_shift()
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
                    await asyncio.sleep(min(wait_sec, 600))
                    continue

                log.info(
                    f"Star gazing listener active all day for "
                    f"{', '.join(STAR_GAZING_GOOD_KEYWORDS)}. "
                    f"Next manifest {self.state['next_star_manifest_time']}."
                )
                await asyncio.sleep(600)

    def is_formation_success(self, text):
            """检测阵法是否已成（周天星斗大阵-成 或 大阵已成）。"""
            return bool(text and ("周天星斗大阵-成" in text or "大阵已成" in text))

    def is_formation_pending(self, text):
            """检测阵法是否正在召集助阵（周天星斗大阵-启 或 尚需 或 助阵）。"""
            return bool(text and ("周天星斗大阵-启" in text or "尚需" in text or "助阵" in text))

    def is_raw_formation_command(self, text):
            """检测是否用户直接输入了 .启阵 指令（不是机器人回复）。"""
            return (text or "").strip() == ".启阵"

    def is_formation_cooldown_active(self):
            """检测阵法冷却是否仍然有效（距上次启阵不足 12 小时）。"""
            last_formation = self.state.get("last_formation_time", "")
            return bool(
                last_formation and is_future(add_seconds_str(last_formation, 12 * 3600))
            )

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
                self.pending_formation_invite_msg = None
                return False

            # 助阵失败
            if any(k in assist_text for k in ["无法助阵", "不能助阵", "助阵失败", "已助阵"]):
                log.info(f"Avatar [{avatar}] assist failed: {assist_text[:80]}")
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
                await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=60)
                self.set_avatar_state(avatar, "in_deep_meditation", False)
                self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                self.set_avatar_state(avatar, "next_force_exit_time", "")
                await asyncio.sleep(10)
                resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关", timeout=60)
                resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
                await self.record_avatar_deep_meditation_start(avatar, resp_text)
                log.info(f"Avatar [{avatar}] deep meditation restarted after force exit.")
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
