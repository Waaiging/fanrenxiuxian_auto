#!/usr/bin/env python3
"""
Sub Cultivator v1.0 (Star Palace / 星宫 Edition)
小号修仙脚本 — 专为「凡人修仙传」Telegram 游戏中的星宫角色设计。

模块功能概览：
  - 星辰牵引（天雷星）：36 小时周期拉起星辰，自动处理冷却与修为不足。
  - 星辰安抚：每 6 小时检查观星台，发现黯淡/紊乱立即安抚。
  - 观星与改换星移：监听 Good 级显化事件，在显化后分层发送 .改换星移 以抢占最终结算前窗口。
  - 周天星斗大阵（启阵/助阵）：12 小时冷却，成功后 5h55m 强行出关以利用增益。
  - 深度闭关：自动维持深度闭关状态，出关后重新开启。
  - 元婴出窍：8 小时周期。
  - 探寻裂缝：12 小时周期，检测到虚弱期自动停止脚本并告警。
  - 抚摸法宝（青竹蜂云剑）：2 小时周期。
  - 每日任务：点卯、闯塔。
  - 侍妾管理：自动安置/召回侍妾以配合闭关与星辰操作。
  - 野外历练与宗门战：被动处理，简版逻辑。
  - 低价天雷竹告警：监控万宝楼低价上架并发送通知。
"""

# ============================================================
# 标准库导入
# ============================================================
import asyncio        # 异步 I/O 框架，用于协程任务调度

import functools
def safe_bg_task(func):
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except Exception as e:
            self.log.error(f"Background task {func.__name__} failed: {e}", exc_info=True)
            # Reset states if necessary depending on the task
            if func.__name__ == "delayed_force_exit":
                self.state["formation_active_until"] = ""
                self.state["next_force_exit_time"] = ""
                self.save_state()
            elif func.__name__ == "delayed_avatar_force_exit":
                avatar = args[0] if args else kwargs.get("avatar")
                if avatar:
                    self.set_avatar_state(avatar, "formation_active_until", "")
                    self.set_avatar_state(avatar, "next_force_exit_time", "")
    return wrapper
import time           # 获取单调时钟（性能计时）和时区设置
import json           # 序列化/反序列化配置与状态文件
import os             # 文件路径、环境变量操作
import sys            # 标准输出流重定向
import re             # 正则表达式，用于解析冷却时间、关键词等
import random         # 随机延迟，避免与其他人动作完全同步
import logging        # 日志系统

# 强制设置时区为北京时间，确保所有时间操作基于 Asia/Shanghai
os.environ['TZ'] = 'Asia/Shanghai'
if hasattr(time, 'tzset'):
    time.tzset()
from datetime import datetime, timedelta  # 时间运算核心

# ============================================================
# 第三方库导入
# ============================================================
from telethon import TelegramClient, events  # Telegram MTProto 客户端与事件系统

# ============================================================
# 项目内部模块导入
# ============================================================
from auto_reply_features import is_auto_reply_followup, maybe_auto_reply_exchange
#   自动回复辅助：判断消息是否为自动回复链的一部分，并处理私聊互动

from common_command_features import CommonCommandMixin, common_command_default_state
#   通用指令混入类：提供 send_and_wait_feedback 等共用方法的基础实现与默认状态

from command_feedback import send_and_wait_feedback_common
#   指令反馈等待：封装了发送指令、等待回复、超时重试的通用逻辑

from concubine_features import ConcubineMixin, concubine_default_state
#   侍妾功能混入类：提供侍妾召回/安置/每日问安等方法的默认状态和基础实现

from fishing_features import FishingMixin

from star_gazing_collector import record_star_gazing_event

from log_utils import (
    CommandLogFilter,           # 日志过滤器：将包含指令关键词的日志行额外标记
    cap_command_retries,        # 限制重试次数，防止无限重试
    command_send_allowed,       # 检查指令发送频率是否允许
    command_send_precheck,      # 不记录发送次数的切换前预检
    handle_clear_history_command, # 处理清屏指令
    handle_anti_bot_challenge,  # 处理游戏机器人的反机器人验证
    is_deep_meditation_ongoing_response,       # 检测"闭关进行中"回复
    is_deep_meditation_settlement_response,    # 检测"闭关结算"回复
    is_game_bot_sender,         # 判断发送者是否为游戏机器人
    is_not_deep_meditation_response,          # 检测"未在闭关"回复
    is_yuanying_out_settlement_response,      # 检测元婴/元神归窍结算
    log_edited_message_if_needed,             # 记录被编辑的消息
    log_incoming_message,       # 记录收到的消息
    log_manual_outgoing_if_needed,            # 记录手动（非脚本）发出的消息
    match_pending_edited_feedback,            # 安全匹配编辑后的机器人反馈
    match_pending_feedback_by_message_id,      # 按消息 ID 匹配编辑反馈
    match_pending_feedback_by_reply,           # 按 reply_to 匹配机器人反馈
    log_mention_if_needed,      # 记录被 @ 的消息
    notify_unrecognized_response,             # 上报无法识别的机器人回复
    periodic_log_prune,         # 定期裁剪日志文件，防止无限膨胀
    prune_log_file,             # 工具函数：裁剪日志到指定行数
    record_bot_no_response,     # 记录机器人无响应事件
    record_bot_response,        # 记录机器人成功响应
    record_cultivation_profile_from_text, # 同步境界/修为资料
    record_command_sent,       # 指令台账
    record_game_bot_activity,   # 记录游戏机器人的最后活动时间
    record_message_event,       # 消息事件库
    record_manual_command_reply_state_if_needed, # 同步手动指令回复状态
    recent_profile_identity_for_text, # 识别无 reply 档案回复的身份
    remember_script_send_intent, # 记录脚本即将发送指令的意图
    remember_script_sent_message, # 记录脚本已发送的消息
    schedule_command_auto_delete, # 安排指令自动删除（隐私）
    send_text_alert,            # 发送文本告警到监控群组
    sender_display_name,        # 获取发送者的显示名
    is_edited_message_for_current_account, # 判定消息是否针对当前账号的编辑
    feedback_response_conflicts, # 判定回复文本是否属于其他指令家族
    feedback_response_matches_command, # 判定回复文本是否正向匹配该指令
    feedback_response_requires_positive_match, # 已知指令需要正向内容匹配
    is_reply_to_manual_command,              # 检查是否为手动指令回复
    is_reply_to_untracked_message, # 带 reply_to 但不属于本脚本指令的回复
    maybe_handle_han_soul_choice, # 韩天尊神魂抉择自动回复
    wait_for_bot_activity_before_send,  # 等待机器人活跃后再发送（避免竞态）
    mentions_self,             # 判定消息是否提到了当前账号
    mentions_other_user,        # 判定消息是否明确提到了其他账号
    mentions_other_user_for_identity, # 身份感知的“其他用户”提及判定
    text_targets_current_account,       # 判定机器人文本是否明确指向当前账号
    tracked_command_identity_for_reply, # 识别手动/脚本指令回复对应身份
    tracked_command_text_for_reply, # 识别手动/脚本指令回复对应指令
)

# ============================================================
# 配置文件路径定义
# ============================================================
# 所有配置、日志、状态文件都放在当前脚本所在目录下
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config_sub.json')   # 副号配置文件（API 密钥、目标群组等）
LOG_FILE = os.path.join(CONFIG_DIR, 'sub_cultivator.log')   # 运行时日志
STATE_FILE = os.path.join(CONFIG_DIR, 'state_sub.json')     # 持久化状态（冷却时间、闭关状态等）

# ============================================================
# 时间格式与指令常量
# ============================================================
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # 所有时间字符串的统一格式
MAX_SCHEDULER_SLEEP_SECONDS = 300

# -- 星辰牵引（天雷星） --
STAR_ATTRACTION_TARGET = "天雷星"
STAR_ATTRACTION_COMMAND = f".牵引星辰 {STAR_ATTRACTION_TARGET}"  # 牵引指令，绑定天雷星
STAR_ATTRACTION_COOLDOWN_SECONDS = 36 * 3600       # 牵引冷却 36 小时（一次牵引持续一整轮）
STAR_PRE_APPEASE_LEAD_SECONDS = 60                 # 收集前 1 分钟安抚
STAR_STATUS_RETRY_SECONDS = 10 * 60                # 观星台异常时 10 分钟后复查
STAR_INSUFFICIENT_RETRY_SECONDS = 60 * 60          # 强行出关后仍修为不足，1 小时后重试
STAR_ATTRACTION_AVATARS = {"厚土", "缘生子", "寻真子"}  # 副号参与牵引循环的化身

# -- 星辰安抚 --
STAR_CALM_INTERVAL_SECONDS = 6 * 3600              # 安抚冷却 6 小时（机器人每 6 小时可安抚一次）

# -- 观星与改换星移 --
STAR_GAZING_INTERVAL_HOURS = 3                      # 星盘显现间隔 3 小时（每 3 小时整点一次）
STAR_GAZING_MONITOR_LEAD_SECONDS = 3 * 60           # 在显现前 3 分钟开始监听消息
STAR_GAZING_COMMAND_LEAD_SECONDS = 60               # Good 轮次：整点前 1 分钟发送 .观星，避免改换星移回复超时
STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS = (22, 24)    # 副号在显现后 22~24 秒发出，抢显化后的机缘窗口
STAR_GAZING_SHIFT_LEAD_SECONDS = -STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS[1]  # 负数表示窗口截止在显现后
STAR_GAZING_SHIFT_GRACE_SECONDS = 1                 # 超过配置窗口 1 秒后不再补发，避免结算后无效改换
STAR_GAZING_SHIFT_REPEAT_COUNT = 1                  # 改换星移只发 1 次（晚发策略不需要重试）
STAR_GAZING_SHIFT_REPEAT_INTERVAL_SECONDS = 3       # 重复间隔（虽然只发 1 次，但必须定义否则 schedule_star_shift 会 NameError）
STAR_GAZING_DAILY_FALLBACK_HOUR = 23                # 每日备用观星时间：23:59（当天未观星时的兜底）
STAR_GAZING_DAILY_FALLBACK_MINUTE = 59
STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
# 以上关键字表示 Good 级别的观星结果，只有 Good 才触发观星和改换星移
STAR_GAZING_FATE_RE = re.compile(r"【((?:Good|Bad|Neutral)\s*-\s*[^】]+)】")
STAR_GAZING_ACTIVE_WINDOW_SECONDS = 59  # 活跃抢占期缩短为 59 秒。超过这个时间收到消息直接排期到下一轮
STAR_GAZING_ROTATING_AVATARS = ["厚土", "缘生子", "寻真子"]  # 观星轮换化身列表：每次 Good 事件只派一个化身


def star_gazing_shift_dt(target_dt, now=None):
    """Return a shift send time within this account's post-manifest window."""
    now = now or datetime.now()
    min_delay, max_delay = STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS
    elapsed = (now - target_dt).total_seconds()
    if elapsed > min_delay:
        min_delay = min(max_delay, max(min_delay, int(elapsed) + 1))
    delay = random.randint(min_delay, max_delay)
    return target_dt + timedelta(seconds=delay)

MAIN_STAR_PALACE_STATE_KEYS = {
    "next_star_check_time",
    "next_star_attraction_time",
    "star_attraction_retry_time",
    "next_star_gazing_time",
    "pending_star_gazing_target_time",
    "pending_star_shift_target_time",
    "next_star_appease_time",
    "next_star_collect_time",
}
MAIN_FORMATION_STATE_KEYS = {
    "next_formation_time",
    "next_formation_retry_time",
    "next_force_exit_time",
}
MAIN_CONCUBINE_STATE_KEYS = {
    "next_dream_map_time",
    "next_heart_trial_time",
    "next_divination_time",
    "next_concubine_voyage_time",
}

# -- 元婴出窍 --
YUANYING_OUT_CD_SECONDS = 8 * 3600                  # 元婴出窍冷却 8 小时
YUANYING_RETREAT_COMMAND = ".元婴闭关"              # 副号主魂改用元婴闭关循环

# -- 探寻裂缝 --
RIFT_SEARCH_CD_SECONDS = 12 * 3600                  # 探寻裂缝冷却 12 小时
AVATAR_YUANYING_RIFT_AVATARS = {"缘生子"}           # 启用化身元婴出窍/探寻裂缝

# -- 抚摸法宝 --
TREASURE_TOUCH_COMMAND = ".抚摸法宝 青竹蜂云剑"      # 抚摸本命法宝指令
TREASURE_TOUCH_CD_SECONDS = 2 * 3600                # 抚摸冷却 2 小时

# -- 元婴宗 --
MAIN_SECT_NAME = "元婴宗"
ASK_DAO_COMMAND = ".问道"
ASK_DAO_CD_SECONDS = 12 * 3600
ASK_DAO_RETRY_SECONDS = 10 * 60

# -- 改换星移目标用户名 --
STAR_GAZING_SHIFT_TARGET = "@Gamling33"             # 将星辰转移给此用户（主号或盟友）

# -- 天雷竹低价告警阈值 --
LOW_PRICE_TIANLEIZHU_LIMIT = 300                    # 万宝楼天雷竹价格低于 300 灵石则告警

# -- 侍妾召回提前量 --
CONCUBINE_RECALL_LEAD_SECONDS = 60                  # 在事件触发前 60 秒召回侍妾

# -- 每日任务时间 --
DAILY_TASK_START_HOUR = 7                           # 每日任务在早上 7:15 开始
DAILY_TASK_START_MINUTE = 15

# -- 宗门传功上限 --
SECT_SKILL_MAX_DAILY = 3                            # 每日最多传功 3 次

# ============================================================
# 时间辅助函数
# ============================================================

def now_str():
    """返回当前时间的格式化字符串，例如 '2026-05-22 14:30:00'"""
    return datetime.now().strftime(TIME_FORMAT)

def str_to_dt(s):
    """将时间字符串解析为 datetime 对象；解析失败则返回当前时间（兜底）"""
    try:
        return datetime.strptime(s, TIME_FORMAT)
    except Exception:
        return datetime.now()

def dt_to_str(dt):
    """将 datetime 对象格式化为时间字符串"""
    return dt.strftime(TIME_FORMAT)

def add_seconds_str(s, seconds):
    """
    在时间字符串上增加指定秒数。
    用于计算冷却到期时间，例如 add_seconds_str(now_str(), 3600) 得到一小时后。
    """
    dt = str_to_dt(s)
    return dt_to_str(dt + timedelta(seconds=seconds))

def is_future(s):
    """
    判断时间字符串是否表示未来的时间。
    用于检查冷却是否已过期：如果状态中的"下一次时间"已经过去，说明可以再次操作。
    """
    try:
        return str_to_dt(s) > datetime.now()
    except Exception:
        return False

def seconds_until(s):
    """
    计算从现在到目标时间字符串还有多少秒。
    如果目标时间已过去，返回 0（不会返回负数）。
    """
    try:
        target = str_to_dt(s)
        diff = (target - datetime.now()).total_seconds()
        return max(0, diff)
    except Exception:
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
    计算从当前时间到每日任务开始时间（DAILY_TASK_START_HOUR:DAILY_TASK_START_MINUTE）的秒数。
    如果当前时间已过开始时间，返回 0 表示"立即执行"。
    """
    target = now.replace(
        hour=DAILY_TASK_START_HOUR,
        minute=DAILY_TASK_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        return 0
    return max(1, int((target - now).total_seconds()) + 1)

def daily_task_start_label():
    """返回每日任务开始时间的人类可读标签，例如 '07:15'"""
    return f"{DAILY_TASK_START_HOUR:02d}:{DAILY_TASK_START_MINUTE:02d}"

# ============================================================
# 日志系统初始化
# ============================================================
# 启动前先裁剪旧日志（防止日志无限膨胀）
prune_log_file(LOG_FILE)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),  # 写入文件（追加模式）
        logging.StreamHandler(sys.stdout),                          # 同时输出到终端
    ],
    force=True,
)
log = logging.getLogger('StarPalace_Sub')          # 本模块的日志器，便于区分来源
logging.getLogger('telethon').setLevel(logging.WARNING)  # 压制 Telethon 库的 INFO 日志，仅警告及以上才输出

# ============================================================
# 配置加载函数
# ============================================================

def load_config():
    """从 config_sub.json 加载账号配置（API ID、API Hash、目标群组等）"""
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

# ============================================================
# 日志过滤器：屏蔽无意义的连接断开警告
# ============================================================

class ConnectionFilter(logging.Filter):
    """
    自定义日志过滤器：过滤掉 "Server closed the connection" 消息。
    Telethon 在连接被服务器关闭时会打印此消息，这是正常的重连行为，不应视为错误。
    """
    def filter(self, record):
        msg = record.getMessage()
        return "Server closed the connection" not in msg

# 为所有根日志处理器添加上述过滤器
for handler in logging.root.handlers:
    handler.addFilter(ConnectionFilter())
    if isinstance(handler, logging.FileHandler):
        # 文件日志额外添加 CommandLogFilter，这可能用于标记包含指令的日志行
        handler.addFilter(CommandLogFilter())

# ============================================================
# AtomicTaskContext: 整体性任务独占锁上下文管理器
# ============================================================
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

# ============================================================
# 主类：SubCultivator
# 继承自 CommonCommandMixin（通用指令方法）和 ConcubineMixin（侍妾管理方法）
# ============================================================

class SubCultivator(CommonCommandMixin, ConcubineMixin, FishingMixin):
    """星宫副号修仙脚本主类，管理所有自动循环与事件响应。"""

    yuanying_main_command = YUANYING_RETREAT_COMMAND

    def __init__(self, session_name='sub_account_session'):
        """
        初始化副号脚本实例。
        参数:
            session_name: Telethon session 文件名，用于持久化登录会话。
        """
        # ---- 配置加载 ----
        self.account_key = "sub"
        self.config = load_config()                    # 加载 config_sub.json
        self.mc = self.config.get('monitor', {})       # 监控配置子段

        # ---- Telethon 客户端 ----
        self.session_file = os.path.join(CONFIG_DIR, session_name)
        self.client = TelegramClient(
            self.session_file,
            self.config['api_id'],
            self.config['api_hash']
        )

        # ---- 目标聊天/主题 ----
        self.target_chat_id = self.mc.get('chat_id', 1680975844)   # 游戏群组 ID
        self.topic_id = self.mc.get('topic_id', 7310786)           # 子区（话题）ID

        # ---- 游戏机器人用户名 ----
        self.watch_bot = self.mc.get('watch_bot', 'fanrenxiuxian_bot').lower().lstrip('@')

        # ---- 主魂宗门配置 ----
        self.sect_name = MAIN_SECT_NAME
        self.main_star_palace_enabled = False
        self.main_formation_enabled = False
        self.main_concubine_enabled = True
        self.field_training_command = ".野外历练 谨慎"  # 野外历练指令，谨慎模式

        # ---- 运行状态 ----
        self.is_running = True    # 主循环开关
        self.pause_event = asyncio.Event()  # 暂停/恢复控制（set=运行中, clear=暂停中）
        self.pause_event.set()    # 默认运行中
        # 止/启管理员名单（只有这些人发"止"才生效）
        self.pause_admins = set(self.mc.get("pause_admins", [8615886738, -1004237793558, -1003885521329, -1003340352216]))  # 主魂(Gamling33)+厚土+缘生子+寻真子
        self.my_info = None       # 自身账号信息（启动后填充）
        self.notify_users = [u.lower() for u in self.mc.get('notify_users', [])]  # 要监控的用户
        self.keywords = [k.lower() for k in self.mc.get('keywords', [])]          # 要监控的关键词
        self.notified_alert_ids = set()                                           # 关键词提醒去重

        # ---- 指令反馈事件系统 ----
        # feedback_events: 字典 { 消息ID: asyncio.Event }，用于等待游戏机器人对某条指令的回复
        # last_feedback_text/msg: 存储最近一次匹配到的回复内容/消息对象
        # feedback_commands: 记录每个等待中的指令对应的原始指令文本
        # feedback_sent_ts: 记录发送时间戳，用于超时判断
        # feedback_senders: 记录每条指令的发送者 ID，用于排除命令回声和审计
        # last_sent_id: 记录最近发送的消息 ID，用于排除自己消息的误匹配
        self.feedback_events = {}
        self.last_feedback_text = {}
        self.last_feedback_msg = {}
        self.feedback_commands = {}
        self.feedback_sent_ts = {}
        self.feedback_senders = {}  # msg_id → 发送指令的 sender_id
        self.last_sent_id = None

        # ---- 异步锁（防止竞态） ----
        self.cmd_lock = asyncio.Lock()          # 全局指令锁，确保同一时间只处理一条指令
        self.star_gazing_lock = asyncio.Lock()  # 观星操作专用锁

        # ---- 后台任务引用 ----
        self.star_gazing_task = None   # 观星调度协程
        self.star_shift_task = None    # 改换星移调度协程

        # ---- 启动同步事件 ----
        self.startup_done = asyncio.Event()  # 启动同步完成信号，所有循环等待此信号后才能开始

        # ---- 持久化状态 ----
        self.state_file = STATE_FILE
        self.state = self.load_state()  # 从 state_sub.json 加载或创建默认状态
        self.migrate_main_soul_sect_state()
        # startup: restore paused state
        if self.state.get("is_paused", False):
            self.pause_event.clear()
            log.info("Startup: is_paused=True, entering paused state.")
        # ---- 身外化身系统（炼气境，只做闭关修炼） ----
        self.avatars = ["厚土", "缘生子", "寻真子"]
        self.avatar_usernames = {
            "crayonxxin": "厚土",
            "lvdoumiao": "缘生子",
            "ding303": "寻真子",
        }
        self.identity_usernames = {
            "主魂": ["Gamling33"],
        }
        self.avatar_identities = {
            # sender_id → 化身名称映射（Telegram 频道 ID）
            "-1004237793558": "厚土",
            "-1003885521329": "缘生子",
            "-1003340352216": "寻真子",
        }
        # 化身 chat_id 映射（供 log_utils.log_manual_outgoing_if_needed 使用）
        self._avatar_chat_ids = {
            "-1004237793558": "厚土",
            "-1003885521329": "缘生子",
            "-1003340352216": "寻真子",
        }
        self.avatar_send_lock = asyncio.Lock()     # 化身操作串行锁
        self._current_identity = self.state.get("current_identity", "主魂")
        self._main_confirmed = (self._current_identity == "主魂")  # 启动时若上次为主魂则默认确认，否则强制对齐
        self._switch_lock = asyncio.Lock()
        self._avatar_loop_count = 0  # 化身循环诊断计数；主魂只等待 avatar_send_lock 的实际占用
        self.command_avatar_map = {}               # msg_id → avatar（命令回复归属）
        self.ensure_avatar_states()  # 确保化身状态存在

        # ---- 阵法助阵标志 ----
        self.formation_assist_in_progress = False           # 防止同时助阵多个阵法
        self.formation_self_pending_until = "2000-01-01 00:00:00"  # 防止自己启阵期间去助阵别人（使用字符串保持类型一致）
        self.pending_formation_invite_msg = None            # 化身间助阵：存储待助阵的邀请消息
        self.active_atomic_task = None             # 整体任务独占锁持有任务


    def migrate_main_soul_sect_state(self):
        """副号主魂从星宫迁入元婴宗后，清理主魂星宫专属排程。"""
        changed = False
        if self.state.get("sect_name") != self.sect_name:
            self.state["sect_name"] = self.sect_name
            changed = True
        for key in ("last_ask_dao_time", "next_ask_dao_time"):
            if key not in self.state:
                self.state[key] = ""
                changed = True
        if not self.main_star_palace_enabled:
            for key in (
                "next_star_check_time",
                "next_star_attraction_time",
                "pending_star_shift_date",
                "pending_star_shift_target_time",
                "pending_star_shift_msg_id",
                "last_treasure_touch_time",
                "next_treasure_touch_time",
                "last_treasure_touch_error",
                "last_treasure_touch_error_time",
            ):
                empty_value = 0 if key == "pending_star_shift_msg_id" else ""
                if self.state.get(key) != empty_value:
                    self.state[key] = empty_value
                    changed = True
        if not self.main_formation_enabled:
            for key in (
                "next_formation_time",
                "next_formation_retry_time",
                "next_force_exit_time",
                "formation_active_until",
            ):
                if self.state.get(key):
                    self.state[key] = ""
                    changed = True
        if not self.main_concubine_enabled:
            for key in (
                "last_concubine_status_time",
                "last_concubine_status_msg_id",
                "next_dream_map_time",
                "next_heart_trial_time",
                "next_divination_time",
                "next_concubine_voyage_time",
                "last_concubine_voyage_time",
                "last_concubine_voyage_error",
                "last_concubine_voyage_error_time",
                "concubine_recalled_for_meditation",
                "concubine_recalled_for_force_exit",
                "concubine_recalled_for_star_collection",
                "concubine_recalled_time",
                "concubine_voyage_active",
                "concubine_placed_in_cave",
                "last_concubine_place_time",
                "last_daily_greeting_date",
            ):
                empty_value = False if key.startswith("concubine_recalled") or key in {"concubine_voyage_active", "concubine_placed_in_cave"} else ""
                if key == "last_concubine_status_msg_id":
                    empty_value = 0
                if self.state.get(key) != empty_value:
                    self.state[key] = empty_value
                    changed = True
        if changed:
            self.save_state()


    # ============================================================
    # 身外化身：状态管理
    # ============================================================

    def ensure_avatar_states(self):
        """确保 state 中存在 avatars 化身专属状态区，每个化身独立冷却"""
        if "avatars" not in self.state:
            self.state["avatars"] = {}
        avatar_default = {
            "next_meditation_time": "",    # 下次可执行闭关修炼的时间
            "last_meditation_time": "",    # 上次执行闭关修炼的时间
            "level": "",                   # 当前境界
            "next_field_training_time": "",  # 下次野外历练时间
            "last_field_training_time": "",
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_active": False,
            "yuanying_out_end_time": "",
            "last_rift_search_time": "",
            "next_rift_search_time": "",
            "bushi_wentian_date": "",
            "bushi_wentian_count": 0,
            "bushi_wentian_exchange_count": 0,
            "nickname": "",                # 化身昵称（Dashboard 显示用）
            "in_deep_meditation": False,   # 是否处于深度闭关中
            "deep_meditation_end_time": "", # 深度闭关结束时间
            "deep_meditation_guard_until": "",
            "meditation_restart_pending": False, # 被动结算后等待重开
            "meditation_restart_mode": "",
            "next_meditation_retry_time": "", # 闭关异常/冷却后的重试时间
            "last_tower_date": "",         # 闯塔：记录最后闯塔日期
            "next_dream_map_time": "",     # 入梦寻图：下次可用时间
            "next_heart_trial_time": "",   # 共历心劫：下次可用时间
            "next_divination_time": "",    # 天机代卜：下次可用时间
            "next_concubine_voyage_time": "", # 侍妾远航：下次归来/可出发时间
            "last_concubine_voyage_time": "",
            "concubine_voyage_active": False,
            "last_gazing_date": "",        # 观星：上次观星日期
            "next_star_attraction_time": "",
            "last_star_attraction_time": "",
            "next_star_appease_time": "",
            "last_star_appease_time": "",
            "star_pre_collect_appeased_for": "",
            "next_star_collect_time": "",
            "last_star_collect_time": "",
            "next_star_check_time": "",
            "last_star_observatory_time": "",
            "star_observatory_summary": "",
            "star_observatory_needs_refresh": True,
            "star_attraction_retry_time": "",
            "star_attraction_force_exit_tried": False,
            "star_target": STAR_ATTRACTION_TARGET,
            "current_exp": 0,              # 当前修为
            "total_exp": 0,                # 总修为上限
            "spirit_root": "",             # 灵根
            "last_dianmao_date": "",       # 宗门点卯日期
            "last_daily_date": "",         # 每日任务：记录最后完成日期
            "pending_star_gazing_date": "", # 观星：待执行的观星日期
            "pending_star_gazing_target_time": "", # 观星：待执行的观星目标时间
            "last_formation_time": "",      # 上次启阵成功时间
            "next_formation_time": "",      # 下次可启阵时间
            "next_formation_retry_time": "",# 启阵重试时间
            "next_force_exit_time": "",     # 强行出关时间（增益结束前5分钟）
            "formation_active_until": "",   # 阵法增益持续时间
        }
        # 昵称默认值（首次创建或昵称为空时写入）
        nicknames = {
            "厚土": "新之助",
            "缘生子": "南绿豆",
            "寻真子": "定定",
        }
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
                # 昵称为空时自动填充
                if not self.state["avatars"][name].get("nickname"):
                    self.state["avatars"][name]["nickname"] = nicknames.get(name, "")
                    changed = True
        if changed:
            self.save_state()

    def get_avatar_state(self, avatar):
        """获取指定化身的状态字典"""
        self.ensure_avatar_states()
        return self.state["avatars"].get(avatar, {})

    def set_avatar_state(self, avatar, key, value):
        """设置指定化身的状态并保存"""
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar][key] = value
            self.save_state()

    def update_avatar_states(self, avatar, values):
        """批量更新化身状态，避免连续写入 state 文件。"""
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar].update(values)
            self.save_state()

    def mark_avatar_meditation_restart_pending(self, avatar, source=""):
        a_state = self.get_avatar_state(avatar)
        self.ensure_meditation_guard_from_end_time(a_state)
        a_state["in_deep_meditation"] = False
        a_state["deep_meditation_end_time"] = ""
        if source != "passive settlement":
            a_state["deep_meditation_guard_until"] = ""
        a_state["meditation_restart_pending"] = True
        self.save_state()
        log.info(f"Avatar [{avatar}] meditation restart pending ({source}).")

    def avatar_meditation_needs_attention(self, avatar):
        a_state = self.get_avatar_state(avatar)
        if self.meditation_guard_active_for_state(a_state):
            return False
        retry_time = self.meditation_defer_until(a_state)
        if retry_time and is_future(retry_time):
            return False
        if a_state.get("meditation_restart_pending"):
            return True
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            return not end_time or not is_future(end_time)
        return not a_state.get("deep_meditation_end_time")

    # ---- 消息过滤：判断是否针对本账号 ----

    def text_targets_self(self, msg, text):
        """判断消息是否针对本账号（与凌霄宫/万灵宗一致）"""
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

    def should_send_keyword_alert(self, msg, text):
        """判断是否应发送 BOSS / 关键词告警。"""
        if not text:
            return False
        lower_text = text.lower()
        if not any(k in lower_text for k in self.keywords):
            return False
        return mentions_self(self, msg, text) or any(
            f"@{u}" in lower_text or f"【{u}】" in lower_text
            for u in self.notify_users
        )

    async def send_keyword_alert(self, msg, text, title="副号关键词提醒"):
        """发送关键词告警给用户，并按消息 ID 去重。"""
        msg_id = getattr(msg, "id", None)
        if msg_id in self.notified_alert_ids:
            return False
        if msg_id is not None:
            self.notified_alert_ids.add(msg_id)
            if len(self.notified_alert_ids) > 300:
                self.notified_alert_ids = set(list(self.notified_alert_ids)[-150:])
        sent = await send_text_alert(self, title, text, log)
        if sent:
            log.info(f"Keyword alert sent for msg {msg_id}.")
        return sent

    # ---- 身外化身：被动身份更新 ----

    def update_identity_passively(self, msg):
        """
        从 bot 回复中被动探测当前身份。
        当检测到切换成功的回复时，自动修正 self.current_identity。
        """
        text = msg.text or ""
        if not text:
            return
        if is_reply_to_untracked_message(self, msg):
            return
        # 严格过滤：如果消息有明确的接收人但不是我，一律无视（防止同群串号）
        if not self.text_targets_self(msg, text):
            return
        # 检测切换回主魂（特殊文案：可能不包含"切换"二字）
        if "神念重归主魂肉身" in text or ("主魂" in text and ("成功" in text or "已切换" in text or "当前操控" in text)):
            if self.current_identity != "主魂":
                log.info(f"🔄 Identity passively updated: {self.current_identity} → 主魂")
                self.current_identity = "主魂"
            return
        # 检测切换到分身
        if "切换" in text or "当前操控" in text:
            for avatar in self.avatars:
                if avatar in text and ("成功" in text or "已切换" in text or "当前操控" in text):
                    if self.current_identity != avatar:
                        log.info(f"🔄 Identity passively updated: {self.current_identity} → {avatar}")
                        self.current_identity = avatar
                        self._main_confirmed = False  # 被动化身切换，主魂确认失效
                    return

    def maybe_record_avatar_passive_states(self, msg):
        """解析并记录手动发送指令引发的状态变更（主魂+化身）"""
        text = msg.text or ""
        if not text:
            return
        if is_reply_to_untracked_message(self, msg):
            return

        if self.record_passive_concubine_voyage_response(text):
            log.info("Passive concubine voyage state synced from loose bot message.")
            return

        # 关键过滤：如果消息不针对本账号，直接忽略（防止其他玩家消息污染化身数据）
        recent_identity = recent_profile_identity_for_text(self, text, msg_id=getattr(msg, "id", None))
        if not self.text_targets_self(msg, text) and not recent_identity:
            return

        avatar = recent_identity or None
        attribution_reliable = bool(recent_identity)
        # 优先根据 sender_id 判断发送者（化身有独立 chat_id）
        sender_id = str(getattr(msg, "sender_id", ""))
        target_str = str(self.target_chat_id).replace("-100", "")
        if not avatar and target_str in sender_id:
            avatar = "主魂"
            attribution_reliable = True
        elif not avatar and "4237793558" in sender_id:
            avatar = "厚土"; attribution_reliable = True
        elif not avatar and "3885521329" in sender_id:
            avatar = "缘生子"; attribution_reliable = True
        elif not avatar and "3340352216" in sender_id:
            avatar = "寻真子"; attribution_reliable = True

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
            if "[Avatar: 厚土]" in text: avatar = "厚土"; attribution_reliable = True
            elif "[Avatar: 缘生子]" in text: avatar = "缘生子"; attribution_reliable = True
            elif "[Avatar: 寻真子]" in text: avatar = "寻真子"; attribution_reliable = True
            elif "神念重归主魂肉身" in text or "当前操控：主魂" in text: avatar = "主魂"; attribution_reliable = True
        # 最后 fallback: current_identity（不可靠）
        if not avatar:
            avatar = self.current_identity
            attribution_reliable = False

        # 统一解析境界和修为（主魂+化身都更新）
        # ⚠️ 必须有 attribution_reliable 守卫！
        # current_identity fallback 不可靠——主魂/他人发的指令可能污染化身数据
        # （send_and_wait_feedback_identity 中已独立解析化身境界，不受此守卫影响）
        if attribution_reliable:
            record_cultivation_profile_from_text(
                self, text, identity=avatar, logger=log, source="passive profile"
            )

        now = now_str()
        is_retreat_settlement = "元婴闭关结算" in str(text or "").replace("**", "")

        if avatar == "主魂" or attribution_reliable:
            if is_retreat_settlement and tracked_command_identity_for_reply(self, msg) != "主魂":
                log.info("Ignored untracked 元婴闭关结算; reply is not tied to sub main soul.")
            elif self.record_yuanying_out_active_response(text, source=f"passive {avatar}", identity=avatar):
                self.save_state()
            elif self.record_yuanying_out_settlement_response(text, source=f"passive {avatar}", identity=avatar):
                self.save_state()

        # ---- 闭关相关（主魂+化身） ----
        # 强行出关 / 明确闭关结算 → 清除深度闭关状态
        _is_real_exit = (not is_retreat_settlement) and any(k in text for k in ["强行出关", "出关成功", "已出关", "闭关结束"])
        _is_deep_settlement = (not is_retreat_settlement) and is_deep_meditation_settlement_response(text)
        if _is_real_exit or _is_deep_settlement:
            if avatar == "主魂":
                self.state["in_deep_meditation"] = False
                self.state["deep_meditation_end_time"] = ""
                self.state["deep_meditation_guard_until"] = ""
                self.state["next_meditation_retry_time"] = ""
                self.state["next_meditation_time"] = ""
            else:
                self.mark_avatar_meditation_restart_pending(avatar, "passive exit")
            log.info(f"[{avatar}] passive: closing state cleared (出关).")

        # 深度闭关中 / 预计还需 → 更新深度闭关结束时间
        elif any(k in text for k in ["深度闭关", "预计还需", "闭关修炼"]):
            is_ongoing = any(k in text for k in ["预计还需", "还需"])
            if is_not_deep_meditation_response(text) or is_deep_meditation_settlement_response(text):
                if avatar == "主魂":
                    self.state["in_deep_meditation"] = False
                    self.state["deep_meditation_end_time"] = ""
                    self.state["deep_meditation_guard_until"] = ""
                    self.state["next_meditation_retry_time"] = ""
                    self.state["next_meditation_time"] = ""
                else:
                    self.mark_avatar_meditation_restart_pending(avatar, "passive settlement")
                log.info(f"[{avatar}] passive: deep meditation ended.")
            else:
                cd = self.parse_wait_time(text)
                if cd > 0:
                    end_time = add_seconds_str(now, cd)
                    if avatar == "主魂":
                        self.state.update(self.meditation_active_state_values(end_time, clear_restart=False))
                    else:
                        self.update_avatar_states(avatar, self.meditation_active_state_values(end_time))
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

        # ---- 入梦寻图 ----
        if "当前进度：" in text and "残图" in text and "拼图" in text:
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now, 8 * 3600))
            log.info(f"[{avatar}] passive: dream map success, cooldown 8h.")
        elif "入梦寻图冷却:" in text or "入梦寻图冷却：" in text:
            match = re.search(r"(?:入梦)?寻图冷却[：:]\s*([^\s\n|]+)", text)
            if match:
                val = match.group(1).strip()
                if "可施展" in val or "无" in val or "可用" in val:
                    self.set_avatar_state(avatar, "next_dream_map_time", now)
                else:
                    cd = self.parse_wait_time(val)
                    if cd > 0:
                        self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now, cd + 60))

        # ---- 共历心劫 ----
        if "坠魔心劫" in text and "第一轮" in text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, 10 * 3600))
            log.info(f"[{avatar}] passive: heart trial started, cooldown 10h.")
        elif "心劫余波未散" in text or "心劫冷却" in text:
            val_match = re.search(r"(?:共历)?心劫冷却\s*[：:]\s*\**\s*([^\s\n|]+)", text)
            if val_match:
                val = val_match.group(1).strip()
                if "可施展" in val or "无" in val or "已就绪" in val or "可用" in val:
                    self.set_avatar_state(avatar, "next_heart_trial_time", now)
                else:
                    cd = self.parse_wait_time(val)
                    if cd > 0 and cd < 30 * 3600:
                        self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, cd))
            else:
                cd = self.parse_wait_time(text)
                if cd > 0 and cd < 30 * 3600:
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, cd))

        # ---- 侍妾远航 ----
        if self.record_concubine_voyage_response(text, identity=avatar):
            log.info(f"[{avatar}] passive: concubine voyage state synced.")

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
        """根据消息 sender_id 判断发送身份（主魂/化身）。
        ⚠️ msg=None 时返回 None（而非 "主魂"），让 command_send_allowed
        能 fallback 到 current_identity，确保不同化身的命令守卫 key 互相隔离。"""
        if not msg or not hasattr(msg, "sender_id"):
            return None  # 不返回 "主魂"，让调用方 fallback 到 current_identity
        sid = str(msg.sender_id)
        return self.avatar_identities.get(sid)

    def _state_impending_command_wait(self, state, identity=""):
        """Return seconds until the next command-worthy timestamp for an identity, or -1."""
        if not isinstance(state, dict):
            return -1
        ignored_keys = {
            "next_switch_allowed_time",
            "next_formation_ban_time",
            "sect_war_active_until",
            "formation_active_until",
            "yuanying_out_end_time",
            "pending_star_gazing_date",
            "last_star_shift_date",
        }
        watch_keys = {"deep_meditation_end_time"}
        min_wait = None
        for key, value in state.items():
            if identity == "主魂":
                if not self.main_star_palace_enabled and key in MAIN_STAR_PALACE_STATE_KEYS:
                    continue
                if not self.main_formation_enabled and key in MAIN_FORMATION_STATE_KEYS:
                    continue
                if not self.main_concubine_enabled and key in MAIN_CONCUBINE_STATE_KEYS:
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
            if (
                state.get("last_dianmao_date") != today
                and not self.dashboard_command_paused(".宗门点卯", identity)
                and seconds_until_daily_task_start(datetime.now()) <= 0
            ):
                min_wait = 0 if min_wait is None else min(min_wait, 0)
            if (
                state.get("last_tower_date") != today
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
    # 状态管理
    # ============================================================

    def load_state(self):
        """
        加载持久化状态文件 state_sub.json，若文件不存在或损坏则返回默认状态。
        默认状态包含所有冷却时间、标志位和缓存时间，初始值为远过去的日期（'2000-01-01 00:00:00'）
        表示"从未执行过"。
        """
        past_time = "2000-01-01 00:00:00"
        default_state = {
            "date": "",                           # 当前日期，用于检测是否跨天
            "done": [],                           # 当天已完成的每日任务列表
            "sect_skill_count": 0,                # 宗门传功今日已使用次数
            "last_formation_time": past_time,     # 上次启阵成功时间
            "last_gazing_date": "",               # 上次观星日期
            "last_gazing_time": past_time,        # 上次观星时间
            "star_attraction_start_time": past_time,   # 上次牵引星辰时间
            "last_calm_time": past_time,          # 上次安抚星辰时间
            "last_collection_time": past_time,    # 上次收集精华时间
            "in_deep_meditation": False,          # 是否处于深度闭关中
            "deep_meditation_end_time": "",       # 深度闭关结束时间
            "deep_meditation_guard_until": "",
            "formation_active_until": past_time,  # 阵法增益有效期截止时间
            "next_force_exit_time": "",           # 下一次强行出关时间（增益结束前 5 分钟）
            "next_star_check_time": past_time,    # 下一次检查观星台时间
            "next_star_attraction_time": "",      # 下一次牵引星辰时间（冷却到期）
            "next_formation_retry_time": past_time,   # 启阵重试时间
            "next_meditation_retry_time": "",     # 深度闭关重试时间
            "next_star_gazing_time": "",          # 下一次观星时间
            "next_star_manifest_time": "",        # 下一次星盘显现时间
            "last_star_shift_date": "",           # 上次改换星移日期
            "last_star_shift_time": "",           # 上次改换星移时间
            "pending_star_shift_date": "",        # 待执行的改换星移日期
            "pending_star_shift_target_time": "", # 待执行的改换星移目标时间
            "pending_star_shift_msg_id": 0,       # 触发改换星移的观星消息 ID
            "pending_star_gazing_time": "",       # 待执行的观星时间
            "pending_star_gazing_date": "",       # 待执行的观星日期
            "pending_star_gazing_target_time": "", # 待执行的观星目标时间
            "pending_star_gazing_scheduled_time": "",  # 已排期的观星时间
            "pending_star_gazing_manifest_time": "",  # 待观星对应的显化整点
            "star_gazing_claimed_manifest_time": "",  # 本账号已指派观星的显化整点
            "star_gazing_claimed_avatar": "",     # 本轮显化已指派的身份
            "last_star_gazing_fallback_date": "", # 上次备用观星日期
            "concubine_placed_in_cave": False,    # 侍妾是否已安置在洞府
            "concubine_recalled_for_meditation": False,    # 是否为闭关召回了侍妾
            "concubine_recalled_for_force_exit": False,    # 是否为强行出关召回了侍妾
            "concubine_recalled_for_star_collection": False, # 是否为星辰收集召回了侍妾
            "concubine_recalled_time": "",        # 侍妾召回时间
            "last_concubine_place_time": "",      # 上次安置侍妾时间
            "last_daily_greeting_date": "",       # 上次每日问安日期
            "is_paused": False,                   # 脚本是否被暂停（"止"指令）
            "last_yuanying_out_time": "",         # 上次元婴出窍时间
            "next_yuanying_out_time": "",         # 下次元婴出窍时间
            "yuanying_out_active": False,         # 元婴出窍是否正在生效
            "yuanying_out_end_time": "",          # 元婴出窍结束时间
            "last_rift_search_time": "",          # 上次探寻裂缝时间
            "next_rift_search_time": "",          # 下次探寻裂缝时间
            "last_treasure_touch_time": "",       # 上次抚摸法宝时间
            "next_treasure_touch_time": "",       # 下次抚摸法宝时间
            "last_ask_dao_time": "",              # 上次问道时间
            "next_ask_dao_time": "",              # 下次问道时间
            "level": "",
            "current_exp": None,
            "total_exp": None,
            "spirit_root": ""
        }
        # 合并通用指令和侍妾功能的默认状态字段
        default_state.update(common_command_default_state())
        default_state.update(concubine_default_state())

        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                    # 字段迁移：如果存在旧的 next_deep_meditation_time 且新的 deep_meditation_end_time 为空，则进行迁移
                    # 这是为了兼容旧版本状态文件的字段命名变化
                    if "next_deep_meditation_time" in s and s["next_deep_meditation_time"]:
                        if not s.get("deep_meditation_end_time"):
                            s["deep_meditation_end_time"] = s["next_deep_meditation_time"]

                    for k in default_state:
                        # 如果字段缺失或者特定字段的值为空字符串，则设为默认值
                        # 这样当状态文件部分损坏时仍能安全恢复
                        if k not in s or (
                            isinstance(s[k], str) and not s[k]
                            and k in [
                                "last_formation_time", "last_calm_time",
                                "last_collection_time", "formation_active_until",
                                "next_star_check_time", "last_gazing_time",
                                "star_attraction_start_time", "next_formation_retry_time"
                            ]
                        ):
                            s[k] = default_state[k]
                    # 兼容旧版字段：如果 next_formation_retry_time 在未来，也设置 next_formation_time
                    retry_time = s.get("next_formation_retry_time", "")
                    if retry_time and is_future(retry_time):
                        s["next_formation_time"] = retry_time
                    self.ensure_meditation_guard_from_end_time(s)
                    return s
            except Exception:
                pass  # 文件损坏或解析失败时忽略异常，返回默认状态
        return default_state

    def save_state(self):
        """
        将当前运行时状态持久化到 state_sub.json。
        每次状态变更后调用，保证脚本重启后能恢复现场。
        """
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Save State Error: {e}")

    # ============================================================
    # 消息发送与删除基础方法
    # ============================================================

    async def send_to_game(self, message, reply_to=None):
        """
        向游戏群组发送一条消息。
        流程:
            1. 等待游戏机器人活跃（避免发送时机器人离线）。
            2. 检查发送频率限制。
            3. 记录发送意图并实际发送。
            4. 记录已发送消息并安排自动删除。
        """
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused(getattr(self, "current_identity", "主魂"), message):
            return None

        try:
            target_reply = reply_to.id if hasattr(reply_to, "id") else (reply_to if reply_to else self.topic_id)
            # 等待机器人活跃，防止消息发送后被机器人忽略
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
            # 检查发送频率是否允许（防止太频繁被限流）
            if not command_send_allowed(self, message, log):
                return None
            # 记录发送意图（用于去重检测）
            remember_script_send_intent(self, message)
            msg = await self.client.send_message(
                self.target_chat_id, message, reply_to=target_reply
            )
            # 记录已发送消息（用于匹配回复）
            remember_script_sent_message(self, msg)
            record_command_sent(self, msg, message, identity=getattr(self, "current_identity", "主魂"), source="auto", reply_to=target_reply, logger=log)
            # 安排消息自动删除（减少隐私暴露）
            schedule_command_auto_delete(self, msg, text=message, logger=log)
            log.info(f"🟢 OUT:\n{message}")
            return msg
        except Exception as e:
            log.error(f"Send Error [{message}] reply_to={target_reply}: {e}")
            return None

    async def delete_msg(self, *msgs):
        """删除一条或多条消息。接受消息对象或消息 ID。"""
        # 统一处理：无论是消息对象还是 ID，都提取为 int
        to_delete = [m.id if hasattr(m, 'id') else m for m in msgs if m]
        if not to_delete:
            return
        try:
            await self.client.delete_messages(self.target_chat_id, to_delete)
        except Exception as e:
            log.warning(f"Delete Error: {e}")

    async def _send_and_wait_feedback_raw(self, message, timeout=45, max_retries=2,
                                          reply_to=None, return_msg=False,
                                          return_sent=False, delete_after=True,
                                          return_response_msg=False,
                                          suppress_no_response_alert=False):
        """内部发送方法（不获取 avatar_send_lock，已被外部调用方持有）"""
        try:
            return await send_and_wait_feedback_common(
                self,
                log,
                message,
                timeout=timeout,
                max_retries=max_retries,
                reply_to=reply_to,
                return_msg=return_msg,
                return_sent=return_sent,
                delete_after=delete_after,
                return_response_msg=return_response_msg,
                return_msg_role="response",
                suppress_no_response_alert=suppress_no_response_alert
            )
        except Exception as e:
            log.error(f"_send_and_wait_feedback_raw [{message[:40]}] crashed: {e}")
            return None

    async def send_and_wait_feedback(self, message, timeout=45, max_retries=2,
                                      reply_to=None, return_msg=False,
                                      return_sent=False, delete_after=True,
                                      return_response_msg=False,
                                      suppress_no_response_alert=False,
                                      force_identity_check=False,
                                      force_meditation_check=False):
        """
        发送指令并等待游戏机器人的回复反馈。
        这是本脚本中最核心的通信方法，几乎所有自动化操作都通过此方法完成。

        逻辑：
          1. 发送指令到游戏群组。
          2. 注册一个事件监听器，等待机器人回复与该指令关联的消息。
          3. 如果在 timeout 秒内收到匹配的回复，返回回复内容。
          4. 如果超时且 max_retries > 0，自动重试。
          5. 支持返回发送消息和/或回复消息对象。

        参数:
            message: 指令文本（如 ".观星台"）。
            timeout: 等待回复的超时秒数。
            max_retries: 超时后的重试次数。
            reply_to: 回复的目标消息 ID。
            return_msg: 是否返回回复消息对象（而不是仅返回文本）。
            return_sent: 是否返回发送消息对象。
            delete_after: 完成后是否删除发送的指令消息。
            return_response_msg: 是否以消息对象形式返回回复（与 return_msg 类似但语义不同）。
            suppress_no_response_alert: 超时无响应时是否静默（不触发告警）。

        返回:
            根据参数不同，返回回复文本、回复消息对象、发送消息对象或 None。
        """
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        if str(message or "").strip() == ".查看闭关" and not force_meditation_check:
            guarded_resp = self.early_meditation_check_response_for_state("主魂", self.state, log)
            if guarded_resp:
                return guarded_resp

        # 暂停守卫：暂停期间阻塞所有自动发送
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
                    if not force_identity_check and self.current_identity in self.avatars:
                        wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                        if not should_yield and 0 <= wait_sec <= 60:
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
                        passively_confirmed = self.current_identity == "主魂"
                        if passively_confirmed:
                            log.info("✅ Auto-switch back to 主魂 passively confirmed.")
                        if (not passively_confirmed) and (not resp_str or not any(k in resp_str for k in ["成功", "已切换", "当前操控", "主魂"])):
                            log.error(f"❌ Auto-switch back to 主魂 FAILED! Blocking main command: {message}. Response: {resp_str[:120]}")
                            return None
                        self.current_identity = "主魂"
                        self._main_confirmed = True
                        switched_this_iteration = True
                        log.info("✅ Auto-switch back to 主魂 confirmed; sending pending command immediately.")

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
                    return await self._send_and_wait_feedback_raw(
                        message,
                        timeout=timeout,
                        max_retries=max_retries,
                        reply_to=reply_to,
                        return_msg=return_msg,
                        return_sent=return_sent,
                        delete_after=delete_after,
                        return_response_msg=return_response_msg,
                        suppress_no_response_alert=suppress_no_response_alert,
                    )

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

    # ============================================================
    # 冷却时间解析工具
    # ============================================================

    def parse_wait_time(self, text, line_identifier=None):
        """
        从游戏机器人的回复文本中解析冷却/剩余时间。
        支持 "X小时"、"X分钟"、"X秒" 格式（中文或英文单位）。

        参数:
            text: 机器人回复文本。
            line_identifier: 可选，如果指定，只解析包含此关键字的行。
                            例如 line_identifier="剩余" 只解析带"剩余"的行。

        返回:
            秒数（int），如果没有找到时间信息则返回 -1。

        逻辑:
            1. 去掉 Markdown 粗体和空格。
            2. 逐行扫描，在每行中用正则匹配小时/分钟/秒。
            3. 如果指定了 line_identifier，跳过不包含它的行。
            4. 返回第一个匹配行的时间总和。
        """
        if not text:
            return -1
        clean = text.replace('**', '').replace(' ', '')
        timed_list = []
        for line in clean.split('\n'):
            if line_identifier and line_identifier not in line:
                continue
            h = re.search(r'(\d+)(?:小时|h)', line)
            m = re.search(r'(\d+)(?:分钟|分|m)', line)
            s = re.search(r'(\d+)(?:秒|s)', line)
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
        return timed_list[0] if timed_list else -1


    # ============================================================
    # 身外化身：发送指令（带身份切换）
    # ============================================================

    async def send_and_wait_feedback_identity(self, identity, message, timeout=45, max_retries=2, **kwargs):
        """
        带身份感知的指令发送：先切换到目标化身，再发送指令。
        使用 avatar_send_lock 确保同一时间只有一个化身在操作。
        """
        force_meditation_check = bool(kwargs.pop("force_meditation_check", False))
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
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
        force_identity_check = bool(kwargs.pop("force_identity_check", False))
        high_priority_identity_command = self.time_critical_identity_command(message)
        allow_unconfirmed_switch = str(message).startswith(".改换星移")

        _t0 = time.monotonic()
        log.info(f"[DEBUG-IDENTITY] [{identity}] ENTER send_and_wait_feedback_identity, cmd={message!r}, lock_held={self.avatar_send_lock.locked()}")
        yield_attempts = 0
        defer_started_at = None
        urgent_yield_attempts = 0
        urgent_defer_started_at = None
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            switched_this_iteration = False
            lock_wait_start = time.monotonic()
            async with self.avatar_send_lock:
                _lock_wait = time.monotonic() - lock_wait_start
                if _lock_wait > 5:
                    log.info(f"[DEBUG-IDENTITY] [{identity}] avatar_send_lock acquired after {_lock_wait:.1f}s (long wait)")
                else:
                    log.info(f"[DEBUG-IDENTITY] [{identity}] avatar_send_lock acquired in {_lock_wait:.1f}s")

                # 如果当前不在目标身份，先切换
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
                        switch_target = "主魂" if identity == "主魂" else identity
                        switch_cmd = f".切换 {switch_target}"
                        log.info(f"🔄 Avatar switch: {self.current_identity} → {identity}")
                        log.info(f"[DEBUG-IDENTITY] [{identity}] sending switch cmd: {switch_cmd}")
                        switch_resp = await self._send_and_wait_feedback_raw(
                            switch_cmd,
                            timeout=5 if high_priority_identity_command else 30,
                            max_retries=0 if high_priority_identity_command else 2,
                            suppress_no_response_alert=high_priority_identity_command,
                        )
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        log.info(f"[DEBUG-IDENTITY] [{identity}] switch response: {resp_str[:120]!r}")

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
                                log.error(f"❌ Avatar switch to {identity} FAILED! Response: {resp_str[:120]}")
                                return None

                        self.current_identity = identity
                        self._main_confirmed = (identity == "主魂")
                        switched_this_iteration = True
                        log.info(f"✅ Avatar switch confirmed: now {identity}; sending pending command immediately.")
                else:
                    log.info(f"[DEBUG-IDENTITY] [{identity}] already in correct identity, skip switch")

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
                    # 等待反馈
                    log.info(f"[DEBUG-IDENTITY] [{identity}] sending cmd: {message!r}")
                    resp = await self._send_and_wait_feedback_raw(message, timeout=timeout, max_retries=max_retries, **kwargs)
                    resp_preview = (getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else "")[:80]
                    log.info(f"[DEBUG-IDENTITY] [{identity}] cmd response: {resp_preview!r}")

                    # 尝试解析境界：仅在化身身份确认时解析
                    # 使用 current_identity 和 avatar state 双重验证，防止主魂/化身境界混淆
                    if resp and identity in self.avatars and self.current_identity == identity:
                        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
                        if resp_text:
                            record_cultivation_profile_from_text(
                                self, resp_text, identity=identity, logger=log, source=f"identity {message}"
                            )

                    _total = time.monotonic() - _t0
                    log.info(f"[DEBUG-IDENTITY] [{identity}] EXIT send_and_wait_feedback_identity, total={_total:.1f}s")
                    return resp

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

    async def switch_back_to_main(self, force=False):
        """
        兼容旧调用的主魂切换钩子。

        默认不主动切回主魂；真正需要发送主魂指令时，send_and_wait_feedback()
        会在指令预检通过后再对齐身份，避免流程收尾阶段产生无后续指令的空切换。
        """
        if self.avatar_send_lock.locked():
            return
        if self._main_confirmed or self.current_identity == "主魂":
            return
        if not force:
            return
        if self.identity_pause_seconds("主魂") > 0:
            log.info("switch_back_to_main skipped: main soul is paused.")
            return
        # 整体任务守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        if self._main_confirmed:
            return  # 已确认在主魂，跳过
        async with self._switch_lock:
            if self._main_confirmed:
                return  # 加锁后再检查——其他任务可能已完成切换
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
                    resp = await self._send_and_wait_feedback_raw(".切换 主魂", timeout=20, max_retries=0)
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
    # 宗门传功响应解析
    # ============================================================



    def record_sect_skill_response(self, resp):
        """
        解析 .宗门传功 的机器人回复，更新今日传功计数。

        返回:
            "counted"  — 成功计数（次数+1或刷新了已有计数）
            "done"     — 今日次数已满（3/3 或提示"次数不足"）
            "invalid"  — 回复目标无效（如"需回复"、"主魂"等）
            "unknown"  — 无法识别的回复，告警并标记为 done

        逻辑:
            优先匹配 "今日已传功 X/3" 格式来刷新计数。
            如果匹配到"次数不足/明日再来"则直接标记为满。
            匹配"失败/需回复/主魂"则说明传功目标不对。
            匹配"成功/传功玉简已记录"等则计数 +1。
        """
        return self.common_record_sect_skill_response(resp, max_daily=SECT_SKILL_MAX_DAILY)

    # ============================================================
    # 星盘显现时间计算
    # ============================================================

    def next_star_manifest_dt(self, now=None):
        return self.common_next_star_manifest_dt(
            now or datetime.now(),
            interval_hours=STAR_GAZING_INTERVAL_HOURS,
        )

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

    def is_loose_meditation_feedback_candidate(self, command, text):
        """
        宽松匹配：当游戏回复不是直接 reply_to 且不含 @提及时，通过关键词模糊匹配。
        匹配层级：
          1. 特定指令精确匹配（.查看闭关、.切换、.启阵）
          2. 通用冷却/响应关键词兜底（所有 . 指令）
        """
        if not text or not command:
            return False
        # 层级1：特定指令精确匹配
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
        if command.startswith(".切换 "):
            parts = command.strip().split()
            if len(parts) >= 2:
                target = parts[1]
                has_success = any(k in text for k in ["成功", "已切换", "当前操控", "神念重归", "切换成功"])
                if has_success and target in text:
                    return True
        if command in {".启阵", ".助阵"}:
            formation_keywords = [
                "冷却", "再次启阵", "心神消耗", "参与过布阵",
                "周天星斗大阵", "布设大阵",
                "助阵", "同门相助", "60秒",
                "修为不足", "正在布阵",
            ]
            return any(k in text for k in formation_keywords)
        if command == ASK_DAO_COMMAND:
            return self.is_ask_dao_response(text)
        # 层级2：通用冷却/响应关键词兜底（所有 . 指令）
        if command.startswith("."):
            general_keywords = [
                "冷却", "冷却中", "冷却时间",
                "后再", "后再试", "方可",
                "剩余", "不足", "无法", "尚未",
                "已达上限", "已经", "已完成", "已经完成",
                "修为不足", "境界不足",
                "正在", "进行中",
                "成功", "获得", "增加了", "减少了",
                "心浮气躁", "需要打坐", "调息",
                "未知指令", "不存在",
            ]
            return any(k in text for k in general_keywords)
        return False

    def star_observatory_needs_calm(self, text):
        return self.common_star_observatory_needs_calm(text)

    def low_price_tianleizhu_price(self, text):
        """
        从万宝楼上架消息中解析天雷竹价格。
        如果消息包含 "天雷竹"、"上架至万宝楼"、"【灵石】" 三个关键字，
        则提取价格数字。如果价格低于 LOW_PRICE_TIANLEIZHU_LIMIT 则返回价格，否则返回 None。
        """
        if not text:
            return None
        if not all(k in text for k in ["天雷竹", "上架至万宝楼", "【灵石】"]):
            return None

        clean = text.replace(" ", "")
        match = re.search(r"【灵石】\s*[xX×*＊]?\s*(\d+)", clean)
        if not match:
            return None
        price = int(match.group(1))
        return price if price < LOW_PRICE_TIANLEIZHU_LIMIT else None

    # ============================================================
    # 低价天雷竹告警
    # ============================================================

    async def maybe_alert_low_price_tianleizhu(self, msg, text, sender):
        """
        当检测到低价天雷竹上架时，发送告警到监控群组。
        使用已告警 ID 集合去重，避免同一消息重复告警。
        集合超过 300 条时裁剪到最近 150 条，防止内存泄漏。
        """
        if not sender or not is_game_bot_sender(self, sender):
            return False
        price = self.low_price_tianleizhu_price(text)
        if price is None:
            return False

        # 去重：记录已告警的消息 ID
        seen = getattr(self, "_low_price_tianleizhu_alerted_ids", None)
        if seen is None:
            seen = set()
            self._low_price_tianleizhu_alerted_ids = seen
        if msg.id in seen:
            return True  # 已告警过，跳过
        seen.add(msg.id)
        if len(seen) > 300:
            self._low_price_tianleizhu_alerted_ids = set(list(seen)[-150:])

        # 构造告警文本
        sender_name = sender_display_name(sender, msg)
        alert = (
            f"检测到低价天雷竹上架。\n"
            f"价格：{price} 灵石\n"
            f"发送者：{sender_name}\n"
            f"消息ID：{msg.id}\n\n"
            f"{text}"
        )
        await send_text_alert(self, "万宝楼低价天雷竹", alert, log)
        log.warning(f"Low-price Tianlei Bamboo listing alert sent for msg {msg.id}, price={price}.")
        return True

    # ============================================================
    # 改换星移 / 观星状态查询
    # ============================================================

    def star_shift_done_today(self, today=None):
        return self.common_star_shift_done_today(today or datetime.now().strftime("%Y-%m-%d"))

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
        pending_gazing_target = self.state.get("pending_star_gazing_target_time", "")
        pending_shift_target = self.state.get("pending_star_shift_target_time", "")
        return bool(
            (pending_gazing_target and is_future(pending_gazing_target))
            or (pending_shift_target and is_future(pending_shift_target))
        )

    def pending_daily_star_gazing_fallback_dt(self, now=None):
        return self.common_pending_daily_star_gazing_fallback_dt(now or datetime.now())

    def clear_pending_star_gazing_schedule(self):
        """清除所有排期中的观星数据。"""
        self.state["pending_star_gazing_date"] = ""
        self.state["pending_star_gazing_target_time"] = ""
        self.state["pending_star_gazing_scheduled_time"] = ""
        self.state["pending_star_gazing_manifest_time"] = ""
        self.state["pending_star_gazing_fate_type"] = ""

    def clear_star_gazing_round_claim(self):
        """清除账号级观星轮次占用。"""
        self.state["star_gazing_claimed_manifest_time"] = ""
        self.state["star_gazing_claimed_avatar"] = ""
        self.state["pending_star_gazing_manifest_time"] = ""
        self.state["pending_star_gazing_fate_type"] = ""

    def clear_stale_star_gazing_claim_before_manifest(self, manifest_dt, sender_info="", text_preview=""):
        """Clear an old claimed .观星 round before scheduling the current manifest."""
        claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
        pending_manifest = self.state.get("pending_star_gazing_manifest_time", "") or claimed_manifest
        if not pending_manifest or not manifest_dt:
            return False
        pending_manifest_dt = str_to_dt(pending_manifest)
        if pending_manifest_dt >= manifest_dt:
            return False

        pending = self.state.get("pending_star_gazing_target_time", "")
        claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
        self.clear_pending_star_gazing_schedule()
        self.clear_star_gazing_round_claim()
        if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
            self.star_gazing_task.cancel()
        self.state["next_star_gazing_time"] = ""
        self.save_state()
        log.info(
            f"Star gazing: cleared stale pending .观星 (was at {pending or 'none'}) "
            f"for manifest {pending_manifest}, claimed by {claimed_avatar or 'none'}; "
            f"handling current manifest {dt_to_str(manifest_dt)}. "
            f"Triggered by {sender_info}: {text_preview}"
        )
        return True

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

    def claimed_star_gazing_pending_due(self, avatar, now=None):
        """Return true when a claimed .观星 send is due and may surface as a passive result."""
        if not avatar:
            return False
        pending = (
            self.state.get("pending_star_gazing_scheduled_time", "")
            or self.state.get("pending_star_gazing_target_time", "")
        )
        pending_dt = str_to_dt(pending)
        if not pending_dt:
            return False
        now = now or datetime.now()
        return now >= pending_dt - timedelta(seconds=1)

    def star_gazing_observer_identity(self, text):
        return self.common_star_gazing_observer_identity(text)

    def claimed_star_gazing_reply_msg_id(self, avatar, msg, text):
        msg_id = getattr(msg, "id", 0)
        if not msg_id:
            return 0

        observer = self.star_gazing_observer_identity(text)
        if observer and observer != avatar:
            log.info(
                f"Star gazing [{avatar}]: ignoring passive result msg {msg_id}; "
                f"observer belongs to {observer}."
            )
            return 0

        command = str(tracked_command_text_for_reply(self, msg) or "").strip()
        identity = tracked_command_identity_for_reply(self, msg) or ""
        if command:
            if command == ".观星" and (not identity or identity == avatar):
                return msg_id
            log.info(
                f"Star gazing [{avatar}]: ignoring passive result msg {msg_id}; "
                f"reply target is [{identity or 'unknown'}] {command!r}."
            )
            return 0

        if observer == avatar:
            return msg_id

        log.info(
            f"Star gazing [{avatar}]: ignoring passive result msg {msg_id}; "
            "not tied to our pending .观星 command."
        )
        return 0

    def maybe_record_passive_claimed_star_gazing_result(self, avatar, manifest_dt, gazing_date, msg, text=""):
        """Treat a passive 星盘显化 message as the result for a due claimed .观星 command."""
        if not avatar or self.get_avatar_state(avatar).get("last_gazing_date") == gazing_date:
            return False
        if not self.claimed_star_gazing_pending_due(avatar):
            return False

        reply_msg_id = self.claimed_star_gazing_reply_msg_id(avatar, msg, text)
        if not reply_msg_id:
            return False

        self.set_avatar_state(avatar, "last_gazing_date", gazing_date)
        self.set_avatar_state(avatar, "last_gazing_time", now_str())
        self.state["last_gazing_date"] = gazing_date
        self.state["last_gazing_time"] = now_str()
        self.clear_pending_star_gazing_schedule()
        self.state["next_star_gazing_time"] = ""
        self.save_state()

        log.info(
            f"Star gazing [{avatar}]: passive .观星 result observed for "
            f"{dt_to_str(manifest_dt)}; marked {gazing_date}."
        )
        if manifest_dt and self.get_avatar_state(avatar).get("last_star_shift_date") != gazing_date:
            asyncio.create_task(self.avatar_schedule_star_shift(avatar, reply_msg_id, manifest_dt, gazing_date))
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

    # ============================================================
    # 改换星移调度（核心策略：延迟发送）
    # ============================================================

    @safe_bg_task
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
            shift_dt = star_gazing_shift_dt(target_dt)
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
                log.info(
                    f"Star gazing: sending {command} repeat {idx}/{STAR_GAZING_SHIFT_REPEAT_COUNT} "
                    f"as reply to .观星 result {reply_msg_id}."
                )
                sent_msg = await self.send_to_game(command, reply_to=reply_msg_id)
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

    @safe_bg_task
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
        shift_dt = star_gazing_shift_dt(target_dt)
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

        self.active_atomic_task = asyncio.current_task()
        log.info(f"🔒 [ATOMIC LOCK] Acquired by StarShift-{avatar}")
        try:
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

            command = f".改换星移 {STAR_GAZING_SHIFT_TARGET}"
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
                log.info(f"🔓 [ATOMIC LOCK] Released by StarShift-{avatar}")

    # ============================================================
    # 观星调度（简化版：用于显化事件触发的观星）
    # ============================================================

    @safe_bg_task
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
            await asyncio.sleep(wait_sec)

        self.active_atomic_task = asyncio.current_task()
        log.info(f"🔒 [ATOMIC LOCK] Acquired by StarGazing-{avatar or '主魂'}")
        try:
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
                resp_msg = await self.send_and_wait_feedback(
                    ".观星",
                    timeout=30,
                    max_retries=0,
                    return_msg=True,
                    delete_after=False,
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
                    current_manifest_dt = manifest_dt
                    if current_manifest_dt is None:
                        current_manifest_hour = (datetime.now().hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
                        current_manifest_dt = datetime.now().replace(
                            hour=current_manifest_hour, minute=0, second=0, microsecond=0
                        )
                    shift_dt = star_gazing_shift_dt(current_manifest_dt)
                    
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
                log.info(f"🔓 [ATOMIC LOCK] Released by StarGazing-{avatar or '主魂'}")



    # ============================================================
    # 每日备用观星（23:59 兜底）
    # ============================================================

    async def maybe_run_daily_star_gazing_fallback(self, now=None):
        """
        检查并执行每日备用观星（23:59 兜底）。
        如果当天一直没有观星机会，在 23:59 发送一次 .观星。

        返回 True 表示执行了操作（包括无响应的情况），False 表示未到时间或条件不满足。
        """
        now = now or datetime.now()
        if not self.main_star_palace_enabled:
            return False
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
            resp_msg = await self.send_and_wait_feedback(
                ".观星",
                timeout=45,
                max_retries=0,
                return_msg=True,
                delete_after=False,
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

    # ============================================================
    # 星盘显化事件处理（实时监听）
    # ============================================================

    async def maybe_handle_star_gazing_opportunity(self, msg, text, sender):
        """
        当收到一条新消息时，检查是否为 Good 级别的星盘显化事件，进行以下处理：

        1. 如果文本包含我们关注的 Good 关键词（STAR_GAZING_GOOD_KEYWORDS），
           调度 .观星 在下一个显化窗口前 1 分钟发送。
        2. 如果文本包含其他 Good 关键词（不在我们的目标列表中），
           且当前有排期中的 .观星，则取消排期以保留每日观星机会给 23:59 兜底。
        3. 如果同一显化轮次后续变成 Bad/Neutral，则取消已排程的 .观星。
        4. 跨日显化按目标显化日/实际发送日判断，避免 0 点机会被前一天记录挡掉。

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
                        text,
                    ):
                        return True
                    log.info(
                        f"Star gazing: manifest {manifest_key} already assigned to {claimed_avatar}; "
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
            return True  # 事件已处理（是 Good 显化，只是不是我们的目标）

    # ============================================================
    # 游戏消息响应处理（主入口）
    # ============================================================

    async def handle_game_response(self, event):
        """
        所有游戏群组新消息的入口处理器。
        处理流程（优先级从高到低）：
          1. 记录手动发出的消息（排除自己的指令）
          2. 反机器人验证处理
          3. 低价天雷竹告警
          4. 被动记录野外历练/宗门战
          5. 自动回复跟随检查
          6. 匹配指令回复（精确或宽松匹配）
          7. 星盘显化事件处理
          8. @ 提醒记录
          9. 自动私聊回复
          10. 外部阵法助阵检测
        """
        try:
            msg = event.message
            text = (msg.text or "")
            sender_id = msg.sender_id

            # ---- 控制指令：止/启（仅管理员可触发） ----
            sender_check = await event.get_sender()
            # chat_id 比较需兼容 Telethon 的 -100 前缀（supergroup）
            _chat_id_match = (msg.chat_id == self.target_chat_id or msg.chat_id == int(f"-100{self.target_chat_id}"))
            if not is_game_bot_sender(self, sender_check) and _chat_id_match:
                if await handle_clear_history_command(self, msg, text, sender_check, log):
                    return
                _sender_id = getattr(msg, "sender_id", None)
                if _sender_id and _sender_id in self.pause_admins:
                    stripped = text.strip()
                    if stripped in ("止", ".止", "0", ".0"):
                        if self.pause_event.is_set():
                            self.pause_event.clear()
                            self.state["is_paused"] = True
                            self.save_state()
                            log.info("⏸️ PAUSE command received. All loops paused.")
                            await self.client.send_message(8219248252, "⏸️ 副号脚本已暂停。发送「1」恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return
                    elif stripped in ("启", ".启", "1", ".1"):
                        if not self.pause_event.is_set():
                            self.pause_event.set()
                            self.state["is_paused"] = False
                            self.save_state()
                            log.info("▶️ RESUME command received. All loops resumed.")
                            await self.client.send_message(8219248252, "▶️ 副号脚本已恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return

            # 记录手动发出的消息（不是脚本发的），用于调试和日志追溯
            record_message_event(self, msg, text=text, sender=sender_check, event_kind="new", direction="raw", logger=log)
            record_star_gazing_event("sub", msg, text, sender=sender_check, logger=log)
            if log_manual_outgoing_if_needed(self, msg, text=text):
                return

            # 获取发送者信息
            sender = await event.get_sender()
            if await maybe_handle_han_soul_choice(self, msg, text, sender, log):
                return
            if is_game_bot_sender(self, sender):
                record_game_bot_activity(self, sender, log)
                self.record_star_gazing_final_report_if_needed(msg, text, source="new message")
                if self.maybe_record_main_yuanying_retreat_settlement_reply(msg, text, source="new message"):
                    self.save_state()
                # 被动身份自愈更新
                self.update_identity_passively(msg)
                manual_reply = is_reply_to_manual_command(self, msg)
                await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                if not manual_reply:
                    self.maybe_record_avatar_passive_states(msg)

            # 反机器人验证：如果机器人发来验证提示，自动处理
            if await handle_anti_bot_challenge(
                self, msg, text, sender, log, title="副号自证告警"
            ):
                return

            # 低价天雷竹监控
            await self.maybe_alert_low_price_tianleizhu(msg, text, sender)
            if is_game_bot_sender(self, sender) and self.should_send_keyword_alert(msg, text):
                await self.send_keyword_alert(msg, text, title="副号关键词提醒")
            # 被动记录野外历练和宗门战消息
            self.maybe_record_field_training_passive(msg, text)
            self.maybe_handle_sect_war_message(msg, text, sender)

            # 如果这条消息属于自动回复链中的后续消息，交给自动回复模块处理
            if is_auto_reply_followup(self, msg, sender=sender):
                return
            if await maybe_auto_reply_exchange(self, event, text=text, sender=sender):
                return

            is_matched = False

            # ---- 第 1 步：精确匹配（回复关系） ----
            is_matched = match_pending_feedback_by_reply(
                self, msg, text, self.is_loose_meditation_feedback_candidate, log, label="[REPLY-FEEDBACK]"
            )

            # ---- 第 2 步：@ 或名字匹配 ----
            # 如果消息提到了我们的用户名或昵称，视为对最近一条等待指令的回复
            if (
                not is_matched
                and self.feedback_events
                and is_game_bot_sender(self, sender)
                and text_targets_current_account(self, msg, text)
            ):
                is_matched = match_pending_edited_feedback(
                    self,
                    msg,
                    text,
                    self.is_loose_meditation_feedback_candidate,
                    log,
                    id_window=30,
                    label="[MENTION-FEEDBACK]",
                )

            # ---- 第 3 步：宽松匹配（用于 .查看闭关） ----
            # 游戏机器人有时不回复指定消息而是发新消息，用宽松规则匹配
            if not is_matched and self.feedback_events and is_game_bot_sender(self, sender):
                # 如果是对手动指令的回复，跳过宽松匹配（防止截胡）
                if is_reply_to_manual_command(self, msg):
                    log.info(f"Manual command response detected in loose match (msg {msg.id}), skipping.")
                    is_matched = True
                else:
                    is_matched = match_pending_edited_feedback(
                        self,
                        msg,
                        text,
                        self.is_loose_meditation_feedback_candidate,
                        log,
                        id_window=30,
                        label="[LOOSE-FEEDBACK]",
                    )

            # ---- 第 4 步：非回复消息处理 ----
            if not is_matched:
                # 星盘显化事件
                if await self.maybe_handle_star_gazing_opportunity(msg, text, sender):
                    return
                if not sender_id:
                    return
                # @ 提醒记录
                log_mention_if_needed(self, msg, text=text, sender=sender)
                # 自动回复（私聊互动）
                if await maybe_auto_reply_exchange(self, event, text=text):
                    return
                # 外部阵法助阵
                if self.should_watch_external_formation() and self.is_external_formation_invite(text):
                    asyncio.create_task(self.maybe_assist_external_formation(msg))
        except Exception as e:
            log.error(f"Error in handle_game_response: {e}")

    # ============================================================
    # 每日任务循环
    # ============================================================
    async def _wait_for_main_identity(self):
        """主循环守卫：只等待正在发送的化身指令完成，不主动切回主魂。"""
        while getattr(self, "is_running", True):
            remaining = self.identity_pause_seconds("主魂")
            if remaining <= 0:
                break
            await asyncio.sleep(max(30, min(int(remaining), 300)))
        while self.avatar_send_lock.locked():
            await asyncio.sleep(1)

    async def run_daily_tasks(self):
        """
        每日任务循环，每天 07:15 开始执行。
        任务列表：
          1. .宗门点卯（签到）
          2. .闯塔（爬塔战斗）

        检测到新的一天时重置任务计数。
        """
        return await self.run_common_daily_tasks_loop(
            seconds_until_daily_task_start,
            daily_task_start_label,
            [".宗门点卯", ".闯塔"],
            pre_loop_func=self._wait_for_main_identity,
            sleep_func=scheduler_sleep_seconds,
            send_kwargs_func=lambda command: {
                "return_sent": True,
                "delete_after": command != ".宗门点卯",
            },
            mark_done_before_send=False,
        )

    def maybe_record_main_yuanying_retreat_settlement_reply(self, msg, text, source="message"):
        """Record passive main-soul .元婴闭关 settlement when it replies to our main command."""
        clean = str(text or "").replace("**", "")
        if "元婴闭关结算" not in clean:
            return False
        if tracked_command_identity_for_reply(self, msg) != "主魂":
            log.info("Ignored untracked 元婴闭关结算; reply is not tied to sub main soul.")
            return False
        return self.record_yuanying_out_settlement_response(
            clean,
            source=f"{source} main reply",
            identity="主魂",
        )

    async def _avatar_yuanying_out_check(self, avatar):
        """化身元婴出窍检查，状态写入化身自己的 state。"""
        return await self.common_avatar_yuanying_out_check(avatar, require_meditation_ready=True)

    async def _avatar_rift_search_check(self, avatar):
        """化身探寻裂缝检查；虚弱结果只暂停触发身份。"""
        return await self.common_avatar_rift_search_check(
            avatar,
            RIFT_SEARCH_CD_SECONDS,
            require_meditation_ready=True,
        )

    async def run_avatar_yuanying_rift_loop(self, avatar, initial_delay=0):
        """Run avatar .元婴出窍 and .探寻裂缝 on their independent cooldowns."""
        return await self.run_common_avatar_yuanying_rift_loop(
            avatar,
            initial_delay=initial_delay,
            sleep_func=scheduler_sleep_seconds,
        )

    async def stop_for_rift_weakness(self, response, identity="主魂", msg=None):
        """
        当探寻裂缝触发了元婴虚弱期时，只暂停触发身份并发送告警。
        这是最严重的错误状态之一，需要人工介入处理。
        """
        identity = str(identity or "").strip() or "主魂"
        pause_until = self.set_identity_pause(identity, 6 * 3600, "肉体破碎/元婴虚弱")
        if identity == "主魂":
            self.state["next_rift_search_time"] = pause_until
        elif identity in self.avatars:
            self.set_avatar_state(identity, "next_rift_search_time", pause_until)
        self.save_state()
        await send_text_alert(
            self,
            "副号探寻裂缝告警",
            f"探寻裂缝触发元婴虚弱期，副号身份【{identity}】已暂停 6 小时；其他身份继续执行。\n\n"
            f"恢复时间：{pause_until}\n\n机器人回复：\n{response}",
            log,
        )
        log.critical(
            f"Rift weakness detected. Identity [{identity}] paused until {pause_until}; "
            f"other identities continue.\n{response}"
        )

    # ============================================================
    # 元婴出窍循环
    # ============================================================

    async def run_yuanying_out_loop(self):
        """
        元婴出窍/主魂元婴闭关循环。
        主魂 .元婴闭关 不固定推算时长；等待机器人结算 reply 后重开。
        """
        await self.startup_done.wait()
        while self.is_running:
            wait_time = await self.common_main_yuanying_out_tick()
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ============================================================
    # 探寻裂缝循环
    # ============================================================

    async def run_rift_search_loop(self):
        """
        探寻裂缝循环。周期 12 小时。
        每次发送 .探寻裂缝 后：
          - 如果触发虚弱期且确实是自己的操作（通过 reply_to 验证），停止脚本并告警。
          - 否则按固定冷却指令逻辑处理。
        """
        await self.startup_done.wait()
        plan = self.rift_search_plan("主魂")
        command = plan.command
        last_key = plan.last_key
        next_key = plan.next_key

        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get(next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Rift search loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(scheduler_sleep_seconds(wait_time))
                continue

            log.info(f"Rift search due: sending {command}.")
            resp_msg = await self.send_and_wait_feedback(
                command,
                timeout=plan.timeout,
                max_retries=plan.max_retries,
                return_response_msg=plan.return_response_msg,
            )
            if resp_msg is None:
                if await self.sleep_after_blocked_command(command, "Rift search"):
                    continue
                log.info("Rift search: no response received, retrying later.")
                await asyncio.sleep(scheduler_sleep_seconds(600))
                continue
            resp_text = resp_msg.text or ""

            # 修为不足处理
            if "修为不足" in resp_text:
                log.warning("Rift search: 修为不足, attempting force exit...")
                await self.send_and_wait_feedback(".强行出关", timeout=30)
                self.state["in_deep_meditation"] = False
                self.save_state()
                await asyncio.sleep(5)
                # 重试一次
                resp_msg = await self.send_and_wait_feedback(
                    command,
                    timeout=plan.timeout,
                    return_response_msg=plan.return_response_msg,
                )
                if resp_msg is None:
                    if await self.sleep_after_blocked_command(command, "Rift search force-exit retry"):
                        continue
                    log.info("Rift search: no response after force exit retry.")
                    await asyncio.sleep(scheduler_sleep_seconds(600))
                    continue
                resp_text = resp_msg.text or ""
                if "修为不足" in resp_text:
                    log.warning("Rift search: still 修为不足 after force exit, pausing 2h")
                    self.state[next_key] = add_seconds_str(now_str(), 2 * 3600)
                    self.save_state()
                    # 重新开启深度闭关
                    await asyncio.sleep(3)
                    med_resp = await self.send_and_wait_feedback(".深度闭关", timeout=60)
                    await self.record_deep_meditation_start(med_resp, "Rift search force-exit restore")
                    continue

            # 检测虚弱期
            if self.is_rift_weakness_response(resp_text):
                # 验证：确认这条回复确实是回复我们的（不是其他人的操作）
                replied_id = None
                if hasattr(resp_msg, 'reply_to') and resp_msg.reply_to:
                    replied_id = getattr(resp_msg.reply_to, 'reply_to_msg_id', None)
                elif hasattr(resp_msg, 'reply_to_msg_id'):
                    replied_id = resp_msg.reply_to_msg_id
                if replied_id and replied_id != getattr(self, 'last_sent_id', None):
                    log.warning(
                        f"Rift weakness detected but reply_to #{replied_id} != our sent msg, "
                        f"likely someone else's. Skipping."
                    )
                    continue
                await self.stop_for_rift_weakness(resp_text, identity="主魂", msg=resp_msg)
                break

            # 常规冷却处理
            self.record_fixed_cd_command_response(
                resp_text, command, last_key, next_key, RIFT_SEARCH_CD_SECONDS
            )
            self.save_state()
            wait_time = seconds_until(self.state.get(next_key, "")) or 600
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ============================================================
    # 抚摸法宝循环
    # ============================================================

    async def run_treasure_touch_loop(self):
        """
        定时抚摸本命法宝器灵的循环。周期 2 小时。
        使用 .抚摸法宝 青竹蜂云剑 指令。
        """
        if not self.main_star_palace_enabled:
            log.info("Treasure touch loop disabled for sub main soul after sect migration.")
            return
        await self.startup_done.wait()
        plan = self.treasure_touch_plan(TREASURE_TOUCH_COMMAND)
        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get(plan.next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(
                    f"Treasure touch loop complete. Sleep {int(min(wait_time, 600))}s."
                )
                await asyncio.sleep(scheduler_sleep_seconds(wait_time))
                continue

            log.info(f"Treasure touch due: sending {plan.command}.")
            resp = await self.send_and_wait_feedback(
                plan.command,
                timeout=plan.timeout,
                max_retries=plan.max_retries,
                force_identity_check=plan.force_identity_check,
            )

            # 修为不足处理
            if "修为不足" in resp:
                log.warning("Treasure touch: 修为不足, attempting force exit...")
                await self.send_and_wait_feedback(".强行出关", timeout=30)
                self.state["in_deep_meditation"] = False
                self.save_state()
                await asyncio.sleep(5)
                # 重试一次
                resp = await self.send_and_wait_feedback(
                    plan.command,
                    timeout=plan.timeout,
                    max_retries=plan.max_retries,
                    force_identity_check=plan.force_identity_check,
                )
                if "修为不足" in resp:
                    log.warning("Treasure touch: still 修为不足 after force exit, pausing 2h")
                    self.state[plan.next_key] = add_seconds_str(now_str(), 2 * 3600)
                    self.save_state()
                    # 重新开启深度闭关
                    await asyncio.sleep(3)
                    med_resp = await self.send_and_wait_feedback(".深度闭关", timeout=60)
                    await self.record_deep_meditation_start(med_resp, "Treasure touch force-exit restore")
                    continue

            self.record_treasure_touch_response(resp)
            self.save_state()
            wait_time = seconds_until(self.state.get(plan.next_key, "")) or 600
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ============================================================
    # 元婴宗问道循环
    # ============================================================

    async def run_ask_dao_loop(self):
        """元婴宗主魂 .问道 循环，每 12 小时一次。"""
        return await self.run_common_ask_dao_loop(
            ASK_DAO_COMMAND,
            sleep_func=scheduler_sleep_seconds,
        )

    # ============================================================
    # 观星监听循环（全天候）
    # ============================================================

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
            fallback_dt = self.pending_daily_star_gazing_fallback_dt(now) if self.main_star_palace_enabled else None

            if pending_gazing_target and pending_gazing_scheduled:
                # 排期已过期：清除，避免 dashboard 一直显示旧日期
                if not is_future(pending_gazing_scheduled):
                    log.info(f"Star gazing: expired pending schedule ({pending_gazing_scheduled}) cleared.")
                    self.clear_pending_star_gazing_schedule()
                    self.clear_star_gazing_round_claim()
                    self.save_state()
                    # 清空后 fall through 到下面的 elif/else 分支
                    pending_gazing_target = ""
                    pending_gazing_scheduled = ""
                else:
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

    # ---- 化身星辰牵引 / 安抚 / 收集 ----

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
            check_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30, max_retries=1)
            check_text = self.response_text(check_resp)
            if is_deep_meditation_ongoing_response(check_text) or "预计还需" in check_text:
                cd = self.parse_wait_time(check_text)
                if cd > 0:
                    self.update_avatar_states(
                        avatar,
                        self.meditation_active_state_values(add_seconds_str(now_str(), cd)),
                    )
                return
            await asyncio.sleep(3)
            cultivation_resp = await self.send_and_wait_feedback_identity(avatar, ".闭关修炼", timeout=45, max_retries=1)
            if self.defer_meditation_after_cultivation_cooldown(
                avatar, self.response_text(cultivation_resp), f"[{avatar}] star restart .闭关修炼"
            ):
                return
            await asyncio.sleep(3)
            deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关", timeout=60, max_retries=1)
            await self.record_avatar_deep_meditation_start(avatar, self.response_text(deep_resp))
        except Exception as e:
            log.error(f"Avatar [{avatar}] failed to restart deep meditation after star action: {e}")

    async def attempt_avatar_star_pull(self, avatar):
        resp = await self.send_and_wait_feedback_identity(
            avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1
        )
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

        retry_resp = await self.send_and_wait_feedback_identity(
            avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1
        )
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
        resp = await self.send_and_wait_feedback_identity(
            avatar, ".安抚星辰", timeout=45, max_retries=1
        )
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
        resp = await self.send_and_wait_feedback_identity(
            avatar, ".收集精华", timeout=45, max_retries=1
        )
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
        if self.avatar_meditation_needs_attention(avatar):
            log.info(f"Avatar [{avatar}] star cycle skipped: meditation needs restart first.")
            return

        async with AtomicTaskContext(self, f"AvatarStarAttraction-{avatar}"):
            for _ in range(5):
                if self.avatar_meditation_needs_attention(avatar):
                    log.info(f"Avatar [{avatar}] star cycle interrupted: meditation needs restart first.")
                    return
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

    # ============================================================
    # 星辰牵引核心逻辑
    # ============================================================

    async def start_star_attraction(self):
        """
        发送 .牵引星辰 天雷星 指令，并处理各种响应情况。

        核心逻辑：
          1. 发送牵引指令。
          2. 如果返回"修为不足"（说明正在深度闭关导致无法使用法力）：
             a. 发送 .强行出关 停止闭关。
             b. 更新状态（in_deep_meditation = False）。
             c. 重试一次牵引。
             d. 重新开启深度闭关（补票）。
          3. 如果返回冷却时间 -> 记录下次可牵引时间。
          4. 如果返回成功关键字 -> 记录牵引开始时间，36 小时后可再次牵引。
          5. 其他情况 -> 告警并 600 秒后重试。
        """
        resp = await self.send_and_wait_feedback(STAR_ATTRACTION_COMMAND)
        if "修为不足" in resp:
            log.warning("Insufficient cultivation detected! Triggering .强行出关...")
            await self.send_and_wait_feedback(".强行出关")
            # 更新状态，防止其他循环误判
            self.state["in_deep_meditation"] = False
            self.save_state()
            await asyncio.sleep(5)
            # 重试一次牵引
            resp = await self.send_and_wait_feedback(STAR_ATTRACTION_COMMAND)
            if resp and any(
                k in resp for k in ["牵引", "凝聚", "成功", "开始", "天雷星", "引星盘"]
            ):
                await self.place_concubine_in_cave("Star attraction restarted after forced exit")
            # 补票：重新开启深度闭关
            log.info("Restarting deep meditation after forced exit...")
            med_resp = await self.send_and_wait_feedback(".深度闭关")
            await self.record_deep_meditation_start(
                med_resp, "Deep meditation restarted after forced exit"
            )
        cd = self.parse_wait_time(resp)
        if "修为不足" in resp:
            pass  # 已在上面处理过
        elif cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余"]):
            self.state["next_star_attraction_time"] = add_seconds_str(now_str(), cd)
            log.info(
                f"Star attraction on CD, next at {self.state['next_star_attraction_time']}."
            )
        elif resp and any(
            k in resp for k in ["牵引", "凝聚", "成功", "开始", "天雷星", "引星盘"]
        ):
            self.state["star_attraction_start_time"] = now_str()
            self.state["next_star_attraction_time"] = add_seconds_str(
                now_str(), STAR_ATTRACTION_COOLDOWN_SECONDS
            )
            log.info(
                f"Star attraction started with Tianlei Star, next at "
                f"{self.state['next_star_attraction_time']}."
            )
            await self.place_concubine_in_cave("Star attraction started")
        else:
            if resp:
                notify_unrecognized_response(
                    self, STAR_ATTRACTION_COMMAND, resp, log, "牵引星辰"
                )
            self.state["next_star_attraction_time"] = add_seconds_str(now_str(), 600)
            self.state["next_star_check_time"] = add_seconds_str(now_str(), 600)
        self.save_state()
        return resp

    def star_attraction_wait_seconds(self):
        """返回距离下次牵引星辰还有多少秒。如果不在冷却中则返回 0。"""
        next_attr = self.state.get("next_star_attraction_time", "")
        if next_attr and is_future(next_attr):
            return seconds_until(next_attr)
        return 0

    # ============================================================
    # 星辰安抚相关方法
    # ============================================================

    def is_star_calm_success(self, text):
        """检测安抚星辰是否成功（成功安抚了狂暴星力）。"""
        return bool(text and any(k in text for k in ["成功安抚", "安抚了", "狂暴星力"]))

    def is_star_calm_noop(self, text):
        """检测安抚星辰是否无操作（没有需要安抚的星辰）。"""
        return bool(text and any(k in text for k in ["没有需要安抚", "无需安抚", "不需要安抚"]))

    def record_star_calm_response(self, text, context):
        """
        记录安抚星辰的结果。
        如果成功或无操作，都记录 last_calm_time 为当前时间（重置 6 小时冷却）。
        如果无法识别，上报告警。
        """
        if self.is_star_calm_success(text):
            self.state["last_calm_time"] = now_str()
            self.save_state()
            return True
        if self.is_star_calm_noop(text):
            self.state["last_calm_time"] = now_str()
            self.save_state()
            log.info(f"Star calm skipped by bot ({context}): no star needs calming.")
            return True
        if text:
            notify_unrecognized_response(self, ".安抚星辰", text, log, context)
        return False

    # ============================================================
    # 深度闭关相关方法
    # ============================================================

    def is_deep_meditation_start_success(self, text):
        """检测深度闭关是否成功开启。"""
        if not text:
            return False
        clean = text.replace("**", "")
        return (
            any(k in clean for k in ["成功", "开启", "已进入深度闭关", "深度闭关状态", "神魂将自行吐纳"])
            or ("已在" in clean and "深度闭关" in clean)
        )

    def concubine_recall_flags(self):
        """返回所有侍妾召回标志位的名称列表。"""
        return [
            "concubine_recalled_for_meditation",
            "concubine_recalled_for_force_exit",
            "concubine_recalled_for_star_collection",
        ]

    def should_keep_concubine_recalled_now(self):
        """检查当前是否需要保持侍妾召回状态（任何活跃事件需要侍妾在外时返回 True）。"""
        return any(self.state.get(k) for k in self.concubine_recall_flags())

    async def recall_concubine(self, reason, flag_key):
        """
        召回侍妾（从洞府召唤到身边）。
        注意：此方法在目前版本中已被绕过（直接日志记录并清除标志），
        因为实际的召回逻辑由 send_and_wait_feedback 内部处理。

        参数:
            reason: 召回原因（仅日志）。
            flag_key: 对应的状态标志字段名。
        """
        log.info(f"{reason}: .召回侍妾 disabled; skipping concubine recall.")
        if flag_key:
            self.state[flag_key] = False
        self.state["concubine_recalled_time"] = ""
        self.save_state()

    async def place_concubine_in_cave(self, reason, force=False):
        """
        将侍妾安置回洞府。
        如果侍妾已在洞府且没有活跃的召回标志位且不是强制，则跳过。

        参数:
            reason: 安置原因（仅日志）。
            force: 是否强制安置（无视已有状态）。
        """
        if not self.main_concubine_enabled:
            log.info(f"{reason}: main concubine flow disabled; skipping .安置侍妾.")
            for key in self.concubine_recall_flags():
                self.state[key] = False
            self.state["concubine_recalled_time"] = ""
            self.save_state()
            return
        has_recall_flag = any(self.state.get(k) for k in self.concubine_recall_flags())
        if self.state.get("concubine_placed_in_cave") and not has_recall_flag and not force:
            return

        log.info(f"{reason}: sending .安置侍妾.")
        await self.send_and_wait_feedback(".安置侍妾")
        self.state["concubine_placed_in_cave"] = True
        self.state["last_concubine_place_time"] = now_str()
        self.state["concubine_recalled_time"] = ""
        for key in self.concubine_recall_flags():
            self.state[key] = False
        self.save_state()

    async def maybe_send_daily_greeting_after_recall(self):
        """
        在召回侍妾后，如果今天是第一次召回，发送每日问安。
        每日问安每日只做一次。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.main_concubine_enabled:
            return
        if self.state.get("last_daily_greeting_date") == today:
            return
        if self.state.get("date") == today and ".每日问安" in self.state.get("done", []):
            self.state["last_daily_greeting_date"] = today
            self.save_state()
            return

        log.info("First concubine recall today. Sending .每日问安.")
        sent_msg = await self.send_and_wait_feedback(
            ".每日问安",
            return_sent=True,
            delete_after=True,
        )
        if sent_msg:
            self.state["last_daily_greeting_date"] = today
            done = self.state.setdefault("done", [])
            if ".每日问安" not in done:
                done.append(".每日问安")
            self.save_state()

    async def ensure_concubine_home_default(self, reason):
        """
        确保侍妾回到洞府（默认状态）。
        如果当前有活跃事件需要侍妾在外，则跳过。
        """
        if self.should_keep_concubine_recalled_now():
            log.info(
                f"{reason}: keeping concubine recalled for an active event window."
            )
            return
        await self.place_concubine_in_cave(reason)

    async def meditation_wait_with_concubine_recall(self, end_time, label="Meditation"):
        """
        在等待闭关结束时，计算需要等待的秒数（加上随机偏移以避免与其他号完全同步）。

        注意：此方法原本设计在等待期间召回侍妾，但当前版本中召回逻辑已被分离。
        当前仅返回等待时间，实际等待由调用方处理。
        """
        protected_until = self.meditation_protected_until({"deep_meditation_end_time": end_time}) or end_time
        remaining = seconds_until(protected_until)
        if remaining <= 0:
            return 0
        return remaining + random.randint(10, 30)

    async def record_deep_meditation_start(self, response_text, place_reason):
        """
        记录深度闭关的开始状态。

        流程:
          1. 检查响应是否表示成功开启深度闭关。
          2. 如果成功，解析冷却时间（剩余秒数）。
          3. 如果直接响应中没有冷却时间，通过 .查看闭关 二次确认。
          4. 记录状态：in_deep_meditation=True, deep_meditation_end_time。
          5. 安置侍妾回洞府（闭关期间不需要侍妾）。

        返回:
            True  — 成功记录闭关状态。
            False — 未能开启深度闭关。
        """
        if not self.is_deep_meditation_start_success(response_text):
            if response_text:
                notify_unrecognized_response(
                    self, ".深度闭关", response_text, log, "深度闭关"
                )
            self.state["in_deep_meditation"] = False
            self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False

        cd = self.parse_wait_time(response_text)
        if cd <= 0:
            # 如果响应中没给冷却时间，通过 .查看闭关 二次确认
            verify_resp = await self.send_and_wait_feedback(".查看闭关")
            verify_cd = self.parse_wait_time(verify_resp)
            if verify_cd > 0 and is_deep_meditation_ongoing_response(verify_resp):
                cd = verify_cd
            else:
                log.warning(
                    "Deep meditation start confirmed, but .查看闭关 did not return "
                    "a remaining time; using 8h fallback."
                )

        self.state.update(
            self.meditation_active_state_values(
                add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600),
                clear_restart=False,
            )
        )
        self.save_state()
        await self.place_concubine_in_cave(place_reason)
        return True

    # ============================================================
    # 星辰牵引循环（观星台检查 + 收集精华 + 安抚）
    # ============================================================

    async def run_star_attraction_loop(self):
        """
        星辰牵引核心循环。

        每 6 小时检查一次观星台状态，执行以下操作：
          1. 检测星辰流是否紊乱/黯淡 -> 立即安抚。
          2. 检测精华是否已凝结 -> 收集精华。
             - 收集前如果星辰流不稳定，先安抚再收集。
             - 收集后检查牵引冷却，若冷却已过则重新牵引。
          3. 检测观星台是否空闲 -> 如果冷却已过则重新牵引。
          4. 计算下次检查时间（取各种冷却时间的最小值，最长不超过 6 小时）。
        """
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            # 检查是否有持久化的等待时间
            next_check = self.state.get("next_star_check_time", "")
            if next_check and is_future(next_check):
                wait_sec = seconds_until(next_check)
                log.info(
                    f"Star Attraction loop waiting until {next_check} ({int(wait_sec)}s)"
                )
                await asyncio.sleep(scheduler_sleep_seconds(wait_sec))
                continue

            log.info("Checking Star Observatory (.观星台)...")
            status = await self.send_and_wait_feedback(".观星台")

            if not status:
                await asyncio.sleep(60)
                continue

            # ---- 解析观星台状态并执行指令 ----
            performed_action = False
            calm_sent_this_cycle = False
            next_wait = None

            # 验证响应是否包含观星台相关关键字，如果不是则可能消息错了
            if not any(
                k in status
                for k in ["观星台", "引星盘", "精华", "空闲", "牵引", "剩余", "紊乱", "黯淡"]
            ):
                notify_unrecognized_response(
                    self, ".观星台", status, log, "观星台状态"
                )
                self.state["next_star_check_time"] = add_seconds_str(now_str(), 600)
                self.save_state()
                await asyncio.sleep(scheduler_sleep_seconds(600))
                continue

            essence_ready = "精华已成" in status
            pending_cd = (
                self.parse_wait_time(status, line_identifier="剩余")
                if "剩余" in status
                else -1
            )

            # 1. 检查是否需要安抚星辰
            unstable_star_flow = self.star_observatory_needs_calm(status)
            need_calm = False
            if unstable_star_flow:
                log.info("Star flow unstable (黯淡/紊乱). Calming immediately.")
                need_calm = True

            if need_calm:
                calm_resp = await self.send_and_wait_feedback(".安抚星辰")
                calm_sent_this_cycle = True
                if self.record_star_calm_response(calm_resp, "安抚星辰"):
                    performed_action = True

            # 2. 精华已就绪 -> 收集
            if essence_ready:
                log.info("Star essence ready. Checking whether pre-collection calm is needed...")
                if unstable_star_flow and not calm_sent_this_cycle:
                    calm_resp = await self.send_and_wait_feedback(".安抚星辰")
                    await asyncio.sleep(3)
                    calm_sent_this_cycle = True
                    self.record_star_calm_response(calm_resp, "采集前安抚")
                elif unstable_star_flow:
                    log.info(
                        "Skipping pre-collection calm; .安抚星辰 already sent in this cycle."
                    )
                else:
                    log.info(
                        "Skipping pre-collection calm; .观星台 has no 黯淡/紊乱 keyword."
                    )

                log.info("Collecting star essence...")
                collect_resp = await self.send_and_wait_feedback(".收集精华")
                if "成功" in collect_resp:
                    log.info("Collection successful! Checking attraction cooldown...")
                    await asyncio.sleep(3)
                    self.state["last_collection_time"] = now_str()
                    attr_wait = self.star_attraction_wait_seconds()
                    if attr_wait > 0:
                        log.info(
                            f"Star attraction cooldown active after collection, "
                            f"waiting {int(attr_wait)}s."
                        )
                        await self.place_concubine_in_cave(
                            "Star collection complete; attraction on cooldown"
                        )
                        next_wait = min(
                            STAR_CALM_INTERVAL_SECONDS,
                            int(attr_wait) + random.randint(5, 15),
                        )
                    else:
                        attr_resp = await self.start_star_attraction()
                        if "修为不足" in attr_resp:
                            next_wait = 1800
                        else:
                            await self.place_concubine_in_cave("Star collection complete")
                            performed_action = True
                elif collect_resp:
                    notify_unrecognized_response(
                        self, ".收集精华", collect_resp, log, "收集精华"
                    )
                    await self.place_concubine_in_cave(
                        "Star collection skipped after unknown response"
                    )
                    next_wait = 600
            elif pending_cd > 0:
                # 精华正在凝结中，等待剩余时间
                log.info(f"Star essence in progress, remaining {pending_cd}s.")
                next_wait = pending_cd + random.randint(3, 8)

            # 3. 检查观星台是否空闲
            if "空闲" in status or "未在牵引" in status:
                attr_wait = self.star_attraction_wait_seconds()
                if attr_wait > 0:
                    log.info(
                        f"Star Attraction idle, but 36h cooldown is active for "
                        f"{int(attr_wait)}s."
                    )
                    next_wait = min(
                        next_wait or STAR_CALM_INTERVAL_SECONDS,
                        int(attr_wait) + random.randint(5, 15),
                    )
                else:
                    log.info("Star Attraction idle. Starting...")
                    attr_resp = await self.start_star_attraction()
                    if "修为不足" in attr_resp:
                        next_wait = 1800
                    else:
                        performed_action = True

            # 4. 计算下次检查时间
            if next_wait is not None:
                pass  # 已有明确等待时间
            elif performed_action:
                next_wait = 60  # 操作后短时间再次检查
            else:
                cd = self.parse_wait_time(status)
                if cd > 0:
                    log.info(f"Star essence condensing. Remaining: {cd}s.")
                    next_wait = cd + 30  # 多留 30 秒余量
                else:
                    next_wait = STAR_CALM_INTERVAL_SECONDS  # 默认 6 小时

            # 对齐牵引冷却
            next_attr = self.state.get("next_star_attraction_time", "")
            if next_attr and is_future(next_attr):
                attr_due_in = seconds_until(next_attr)
                if attr_due_in > 0 and attr_due_in < next_wait:
                    next_wait = int(attr_due_in) + random.randint(5, 15)
                    log.info(
                        f"Next star attraction check scheduled by 36h cooldown in "
                        f"{next_wait}s."
                    )

            # 确保不超出安抚间隔（6 小时）
            last_calm_after = self.state.get("last_calm_time", "2000-01-01 00:00:00")
            calm_due_in = STAR_CALM_INTERVAL_SECONDS - (
                datetime.now() - str_to_dt(last_calm_after)
            ).total_seconds()
            if calm_due_in > 0 and next_wait > calm_due_in:
                next_wait = int(calm_due_in) + random.randint(5, 15)
                log.info(f"Next star calm check scheduled by 6h interval in {next_wait}s.")

            if next_wait > STAR_CALM_INTERVAL_SECONDS:
                next_wait = STAR_CALM_INTERVAL_SECONDS + random.randint(5, 15)
                log.info(f"Next .观星台 check capped by 6h interval in {next_wait}s.")

            self.state["last_star_check_time"] = now_str()
            self.state["next_star_check_time"] = add_seconds_str(now_str(), next_wait)
            self.save_state()
            log.info(
                f"Star Attraction check complete. Next check at "
                f"{self.state['next_star_check_time']} ({next_wait}s)."
            )
            await asyncio.sleep(scheduler_sleep_seconds(next_wait))

    # ============================================================
    # 阵法（周天星斗大阵）相关方法
    # ============================================================

    def is_formation_success(self, text):
        """检测阵法是否已成（周天星斗大阵-成 或 大阵已成）。"""
        return bool(text and ("周天星斗大阵-成" in text or "大阵已成" in text))

    def is_formation_pending(self, text):
        """检测阵法是否正在召集助阵（周天星斗大阵-启 或 尚需 或 助阵）。"""
        return bool(text and ("周天星斗大阵-启" in text or "尚需" in text or "助阵" in text))

    def formation_invite_actor_username(self, text):
        if not text:
            return ""
        match = re.search(r"@([A-Za-z0-9_]+)\s*正在布设大阵", text)
        if not match:
            match = re.search(r"@([A-Za-z0-9_]+)", text)
        return (match.group(1).lower() if match else "")

    def formation_invite_actor_identity(self, text):
        username = self.formation_invite_actor_username(text)
        return (self.avatar_usernames or {}).get(username, "")

    def is_own_formation_invite(self, text):
        """检测阵法邀请是否指向我们自己（通过 @用户名 判断）。"""
        if not text or not self.my_info:
            return False
        username = (getattr(self.my_info, "username", "") or "").lower().lstrip("@")
        return bool(username and f"@{username}" in text.lower())

    def is_external_formation_invite(self, text):
        """
        检测是否为外部（他人）的阵法邀请。
        条件：
          1. 不是"已成功"的阵法消息。
          2. 不是我们的自己的阵法邀请。
          3. 包含 "周天星斗大阵-启"、"正在布设大阵"、"尚需/助阵" 等关键字。
        """
        if not text or self.is_formation_success(text):
            return False
        if self.is_own_formation_invite(text):
            return False
        return (
            "周天星斗大阵-启" in text
            and "正在布设大阵" in text
            and ("尚需" in text or "助阵" in text)
        )

    def is_raw_formation_command(self, text):
        """检测是否用户直接输入了 .启阵 指令（不是机器人回复）。"""
        return (text or "").strip() == ".启阵"

    def is_formation_cooldown_active(self):
        """检测阵法冷却是否仍然有效（距上次启阵不足 12 小时）。"""
        last_formation = self.state.get("last_formation_time", "")
        return bool(
            last_formation and is_future(add_seconds_str(last_formation, 12 * 3600))
        )

    def should_watch_external_formation(self):
        """
        判断是否应该监听外部阵法助阵机会。
        条件：
          1. 不在自己的阵法待执行窗口内（formation_self_pending_until）。
          2. 阵法冷却未激活（距上次成功不到 12 小时不参与助阵）。
        """
        if not self.main_formation_enabled:
            return False
        # 使用字符串时间比较，与代码库其他时间字段保持一致
        if is_future(self.formation_self_pending_until):
            return False
        return not self.is_formation_cooldown_active()

    def is_formation_assist_success(self, text):
        """
        检测文本是否表示阵法助阵成功。
        包含各种可能的成功描述形式。
        """
        if not text:
            return False
        return any(
            k in text
            for k in [
                "助阵成功",
                "成功助阵",
                "参与布阵",
                "加入大阵",
                "大阵已成",
                "周天星斗大阵-成",
                "阵成",
                "成功加入",
                "已参与",
                "已经参与",
                "已助阵",
                "已经助阵",
                "助阵完成",
                "已在阵中",
            ]
        )

    def is_formation_assist_failure(self, text):
        """
        检测文本是否表示阵法助阵失败。
        包含各种可能的失败描述形式。
        """
        if not text:
            return False
        return any(
            k in text
            for k in [
                "没有找到正在召集",
                "阵法已过期",
                "已过期",
                "过期",
                "没有找到",
                "无法助阵",
                "不能助阵",
                "助阵失败",
            ]
        )

    def can_assist_external_formation(self):
        """
        判断当前是否可以助阵外部阵法。
        不允许同时助阵多个阵法，且需要满足 should_watch_external_formation 条件。
        """
        if self.formation_assist_in_progress:
            return False
        if not self.should_watch_external_formation():
            return False
        return True

    async def prepare_avatar_for_formation_assist(self, avatar):
        """助阵前只做安全检查；不会为了助阵提前强行出关。"""
        a_state = self.get_avatar_state(avatar)
        block_until = self.avatar_formation_block_until(avatar, a_state)
        if block_until:
            log.info(f"Avatar [{avatar}] formation assist skipped: formation CD until {block_until}.")
            return False
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            suffix = f" until {end_time}" if end_time else ""
            log.info(f"Avatar [{avatar}] formation assist: trying direct assist while in deep meditation{suffix}; no force exit before success.")
        return True

    def message_effective_time_str(self, msg):
        """
        获取消息的有效时间（编辑时间优先，否则使用发送时间）。
        用于记录阵法成功时的准确时间。
        """
        msg_dt = getattr(msg, "edit_date", None) or getattr(msg, "date", None)
        if not msg_dt:
            return now_str()
        try:
            return dt_to_str(datetime.fromtimestamp(msg_dt.timestamp()))
        except Exception:
            return now_str()

    def record_formation_success(self, formation_time=None):
        """
        记录阵法成功后的状态更新。

        逻辑：
          - 设置 last_formation_time 为当前时间。
          - 设置 formation_active_until = 当前时间 + 6 小时（增益持续期）。
          - 设置 next_force_exit_time = 当前时间 + 5小时55分（增益期结束前 5 分钟强行出关）。
          - 设置 next_formation_time = 当前时间 + 12 小时（冷却期）。
          - 创建强行出关延迟任务。
        """
        formation_time = formation_time or now_str()
        force_delay = 5 * 3600 + 55 * 60  # 5 小时 55 分钟
        self.state["last_formation_time"] = formation_time
        self.state["formation_active_until"] = add_seconds_str(formation_time, 6 * 3600)
        self.state["next_force_exit_time"] = add_seconds_str(formation_time, force_delay)
        self.state["next_formation_time"] = add_seconds_str(formation_time, 12 * 3600)
        self.state["next_formation_retry_time"] = ""
        self.save_state()
        log.info(
            f"Formation successful! Active! Next formation at "
            f"{self.state['next_formation_time']}."
        )
        force_delay_remaining = seconds_until(self.state["next_force_exit_time"])
        if force_delay_remaining > 0:
            asyncio.create_task(self.delayed_force_exit(force_delay_remaining))

    async def get_updated_message(self, msg, delay_sec=120):
        """
        等待一段时间后，重新获取消息的编辑后内容。
        用于阵法：游戏机器人的阵法邀请消息在 2 分钟内会被编辑为最终结果。

        参数:
            msg: 原始消息对象。
            delay_sec: 等待秒数（默认 120 秒）。

        返回:
            更新后的消息对象。
        """
        if not msg:
            return None
        log.info(f"Formation initiated. Waiting {delay_sec}s for edited final result...")
        await asyncio.sleep(delay_sec)
        try:
            updated = await self.client.get_messages(self.target_chat_id, ids=msg.id)
            text = (updated.text or "") if updated else ""
            log.info(f"Formation edited message after wait: {text[:120]}...")
            return updated
        except Exception as e:
            log.warning(f"Formation edited message fetch failed: {e}")
            return msg

    async def get_updated_message_text(self, msg, delay_sec=120):
        """获取消息编辑后的文本内容。"""
        updated = await self.get_updated_message(msg, delay_sec=delay_sec)
        return (updated.text or "") if updated else ""

    async def poll_formation_message_for_success(self, msg, timeout_sec=15, interval_sec=1):
        """
        轮询检查阵法邀请消息是否已被编辑为成功状态。
        用于替代固定等待，更高效地检测阵法结果。

        参数:
            msg: 原始消息对象。
            timeout_sec: 最大轮询时间（秒）。
            interval_sec: 轮询间隔（秒）。

        返回:
            如果检测到成功，返回更新后的消息；否则返回最后获取的消息。
        """
        if not msg:
            return None
        deadline = time.monotonic() + timeout_sec
        updated = msg
        log.info(
            f"Polling formation invite {msg.id} for edited success up to {timeout_sec}s."
        )
        while self.is_running and time.monotonic() <= deadline:
            try:
                latest = await self.client.get_messages(
                    self.target_chat_id, ids=msg.id
                )
                if latest:
                    updated = latest
                    text = latest.text or ""
                    if self.is_formation_success(text) or self.is_formation_assist_success(text):
                        log.info(f"Formation invite {msg.id} edited to success.")
                        return latest
            except Exception as e:
                log.warning(f"Formation invite {msg.id} poll failed: {e}")
                break
            await asyncio.sleep(interval_sec)
        return updated

    # ============================================================
    # 外部阵法助阵
    # ============================================================

    async def maybe_assist_external_formation(self, formation_msg):
        """
        收到外部阵法邀请消息时，检查条件并在满足时立即助阵。

        条件：
          1. 消息在 60 秒内（太老的邀请已经无效）。
          2. can_assist_external_formation 返回 True。
          3. 加 formation_assist_in_progress 锁防止并发。
        """
        if not formation_msg:
            return
        if not self.can_assist_external_formation():
            return
        age = self.message_age_seconds(formation_msg)
        if age > 60:
            log.info(
                f"External formation invite {formation_msg.id} ignored: stale ({int(age)}s)."
            )
            return

        self.formation_assist_in_progress = True
        try:
            log.info(
                f"Detected external formation invite {formation_msg.id}; "
                f"assisting immediately."
            )
            await self.assist_external_formation(formation_msg)
        finally:
            self.formation_assist_in_progress = False

    def message_age_seconds(self, msg):
        """计算消息的年龄（从发送到现在的秒数）。"""
        msg_dt = getattr(msg, "date", None)
        if not msg_dt:
            return 0
        try:
            now_dt = (
                datetime.now(msg_dt.tzinfo)
                if msg_dt.tzinfo
                else datetime.utcnow()
            )
            return max(0, (now_dt - msg_dt).total_seconds())
        except Exception:
            return 0

    async def assist_external_formation(self, formation_msg):
        """
        执行外部阵法助阵的具体逻辑。

        流程:
          1. 检查消息是否还在有效期内。
          2. 发送 .助阵 指令（回复邀请消息）。
          3. 检查直接响应：
             a. 成功 -> 记录阵法成功。
             b. 冷却中 -> 推算上次成功时间并设置状态。
             c. 失败 -> 日志记录。
          4. 如果直接响应未确认，轮询消息编辑确认。
        """
        if not formation_msg:
            return False
        age = self.message_age_seconds(formation_msg)
        if age > 60:
            log.info(
                f"External formation message {formation_msg.id} is stale "
                f"({int(age)}s). Skipping assist."
            )
            return False
        log.info(
            f"Assisting external formation via reply to bot invite message "
            f"{formation_msg.id}."
        )
        assist_msg = await self.send_and_wait_feedback(
            ".助阵",
            reply_to=formation_msg.id,
            timeout=5,
            max_retries=0,
            return_msg=True,
            delete_after=False,
            suppress_no_response_alert=True,
        )
        assist_resp = (assist_msg.text or "") if assist_msg else ""

        # 直接响应确认成功
        if self.is_formation_success(assist_resp) or self.is_formation_assist_success(assist_resp):
            log.info("External formation assist confirmed by direct response.")
            self.record_formation_success(self.message_effective_time_str(assist_msg))
            return True

        # 响应显示冷却中 -> 推算上次时间
        cd = self.parse_wait_time(assist_resp)
        if cd > 0 and any(
            k in assist_resp for k in ["冷却", "参与过布阵", "心神消耗", "再次启阵"]
        ):
            last_exec = datetime.now() + timedelta(seconds=cd) - timedelta(hours=12)
            self.state["last_formation_time"] = dt_to_str(last_exec)
            self.state["formation_active_until"] = add_seconds_str(
                self.state["last_formation_time"], 6 * 3600
            )
            self.state["next_force_exit_time"] = add_seconds_str(
                self.state["last_formation_time"], 5 * 3600 + 55 * 60
            )
            self.state["next_formation_retry_time"] = ""
            self.state["next_formation_time"] = add_seconds_str(now_str(), cd)
            self.save_state()
            log.info(
                f"Assist response indicates formation CD ({cd}s). "
                f"Next: {self.state['next_formation_time']}"
            )
            return False

        # 直接响应显示失败
        if self.is_formation_assist_failure(assist_resp):
            log.warning(
                f"External formation assist failed by direct response: "
                f"{assist_resp[:120]}..."
            )
            return False

        # 轮询检查消息编辑
        updated_msg = await self.poll_formation_message_for_success(
            formation_msg, timeout_sec=15, interval_sec=1
        )
        updated_resp = (updated_msg.text or "") if updated_msg else ""
        if self.is_formation_success(updated_resp) or self.is_formation_assist_success(updated_resp):
            log.info("External formation assist confirmed by edited formation message.")
            self.record_formation_success(self.message_effective_time_str(updated_msg))
            return True

        log.info(
            f"External formation assist not confirmed by direct response or "
            f"edited invite {formation_msg.id}."
        )
        return False

    # ============================================================
    # 阵法 & 深度闭关联动循环
    # ============================================================

    async def run_formation_meditation_loop(self):
        """
        阵法（启阵）与深度闭关联动主循环。

        核心设计：
          1. 尝试 .启阵（12 小时冷却）。
          2. 如果启阵成功，记录时间，设置 5 小时 55 分钟后的强行出关。
             - 强行出关的目的是在阵法增益结束前退出闭关状态，
               从而在增益期内最大化野外历练/星辰操作等的收益。
          3. 常规 .深度闭关 逻辑：
             - 如果缓存中显示闭关正在进行且未到期，跳过检查直接等待。
             - 否则通过 .查看闭关 确认状态。
             - 根据需要执行结算、重新闭关等操作。
          4. 计算循环睡眠时间时，同时考虑阵法 CD 和闭关等待时间，
             取最小值以确保不遗漏任何事件。
        """
        while self.is_running:
            now = datetime.now()

            await self._wait_for_main_identity()

            # ---- 尝试启阵 ----
            can_try_formation = False
            if self.main_formation_enabled:
                last_formation = self.state.get("last_formation_time", "")
                next_retry = self.state.get("next_formation_retry_time", "")

                if not last_formation or not is_future(
                    add_seconds_str(last_formation, 12 * 3600)
                ):
                    if not next_retry or not is_future(next_retry):
                        can_try_formation = True

            if can_try_formation:
                log.info("Attempting Star Formation (.启阵)...")
                # 设置待执行窗口，阻止在此期间去助阵他人
                # 使用字符串时间，与代码库其他时间字段保持一致
                self.formation_self_pending_until = add_seconds_str(now_str(), 60)
                resp_msg = await self.send_and_wait_feedback(
                    ".启阵",
                    timeout=120,
                    return_msg=True,
                    delete_after=False,
                )
                resp = (resp_msg.text or "") if resp_msg else ""

                # 修为不足处理
                if "修为不足" in resp:
                    log.warning("Main loop formation: 修为不足, attempting force exit...")
                    await self.send_and_wait_feedback(".强行出关", timeout=30)
                    self.state["in_deep_meditation"] = False
                    self.save_state()
                    await asyncio.sleep(5)
                    # 重试一次
                    resp_msg = await self.send_and_wait_feedback(
                        ".启阵",
                        timeout=120,
                        return_msg=True,
                        delete_after=False,
                    )
                    resp = (resp_msg.text or "") if resp_msg else ""
                    if "修为不足" in resp:
                        log.warning("Main loop formation: still 修为不足 after force exit, pausing 2h")
                        self.state["next_formation_time"] = add_seconds_str(now_str(), 2 * 3600)
                        self.save_state()
                        # 重新开启深度闭关
                        await asyncio.sleep(3)
                        med_resp = await self.send_and_wait_feedback(".深度闭关", timeout=60)
                        await self.record_deep_meditation_start(med_resp, "Formation force-exit restore")
                        continue

                # 情况 1：直接成功
                if self.is_formation_success(resp):
                    self.record_formation_success(self.message_effective_time_str(resp_msg))

                # 情况 2：等待助阵（pending），等待 2 分钟查看编辑结果
                elif self.is_formation_pending(resp):
                    updated_msg = await self.get_updated_message(resp_msg, delay_sec=120)
                    updated_resp = (updated_msg.text or "") if updated_msg else ""
                    if self.is_formation_success(updated_resp):
                        self.record_formation_success(
                            self.message_effective_time_str(updated_msg)
                        )
                    else:
                        log.info(
                            "Formation still pending after edited-message check. "
                            "Retrying in 10 minutes."
                        )
                        retry_at = add_seconds_str(now_str(), 600)
                        self.state["next_formation_retry_time"] = retry_at
                        self.state["next_formation_time"] = retry_at
                        self.save_state()

                # 情况 3：冷却或其他响应
                else:
                    cd = self.parse_wait_time(resp)
                    if cd >= 0 and ("冷却" in resp or "再次启阵" in resp or "心神消耗" in resp):
                        if cd == 0:
                            retry_after = 60
                            self.state["next_formation_time"] = add_seconds_str(
                                now_str(), retry_after
                            )
                            self.state["next_formation_retry_time"] = add_seconds_str(
                                now_str(), retry_after
                            )
                            self.save_state()
                            log.info("Formation CD reported 0s. Retrying in 60 seconds.")
                            continue
                        # 真正的 CD（通常 12 小时），推算上次成功时间
                        last_exec = (
                            datetime.now() + timedelta(seconds=cd) - timedelta(hours=12)
                        )
                        self.state["last_formation_time"] = dt_to_str(last_exec)
                        self.state["next_formation_retry_time"] = ""

                        # 如果仍在 6 小时增益期内，推算强行出关时间
                        # CD > 6 小时说明增益期还没过
                        if cd > 6 * 3600:
                            active_elapsed = 12 * 3600 - cd
                            remaining_to_force = (5 * 3600 + 55 * 60) - active_elapsed
                            if remaining_to_force > 0:
                                log.info(
                                    f"Detected active formation bonus. "
                                    f"Will force exit in {int(remaining_to_force)}s."
                                )
                                self.state["next_force_exit_time"] = add_seconds_str(
                                    now_str(), remaining_to_force
                                )
                                asyncio.create_task(
                                    self.delayed_force_exit(remaining_to_force)
                                )

                        self.state["next_formation_time"] = add_seconds_str(
                            now_str(), cd
                        )
                        self.save_state()
                        log.info(
                            f"Formation on CD ({cd}s). Next: {self.state['next_formation_time']}"
                        )
                    else:
                        # 未知响应，20 分钟后重试
                        if resp:
                            notify_unrecognized_response(
                                self, ".启阵", resp, log, "启阵"
                            )
                        log.info(
                            f"Formation unknown response: {resp[:50]}... "
                            f"Retrying in 20 minutes."
                        )
                        retry_at = add_seconds_str(now_str(), 1200)
                        self.state["next_formation_retry_time"] = retry_at
                        self.state["next_formation_time"] = retry_at
                        self.save_state()

            # ---- 深度闭关逻辑 ----
            meditation_retry_time = self.meditation_defer_until(self.state)
            if meditation_retry_time and is_future(meditation_retry_time):
                wait_sec = min(300, seconds_until(meditation_retry_time))
                log.info(
                    f"Meditation: deferred until "
                    f"{meditation_retry_time}."
                )
                await asyncio.sleep(wait_sec)
                continue

            if self.ensure_meditation_guard_from_end_time(self.state):
                self.save_state()
            guard_wait = self.meditation_guard_wait_seconds_for_state(self.state)
            end_time_str = self.state.get("deep_meditation_end_time", "")
            if guard_wait > 0:
                # 缓存的闭关结束时间在未来，跳过 .查看闭关，直接等待
                log.info(
                    f"Meditation protected, skipping .查看闭关 for "
                    f"{self.compact_duration_text(guard_wait)}."
                )
                med_cd = guard_wait
                check_resp = "Skip"  # 占位符，避免进入后续解析逻辑
            else:
                log.info("Checking meditation status via .查看闭关...")
                check_resp = await self.send_and_wait_feedback(".查看闭关")
                med_cd = self.parse_wait_time(check_resp)

            med_wait = 300  # 默认等待时间
            allow_short_wait = False

            if check_resp == "Skip":
                # 跳过 .查看闭关，直接等待缓存的闭关结束时间
                med_wait = guard_wait + random.randint(10, 30)
                allow_short_wait = med_wait < 60
            elif med_cd > 0 and is_deep_meditation_ongoing_response(check_resp):
                log.info(f"Deep meditation in progress, remaining {med_cd}s.")
                self.state.update(
                    self.meditation_active_state_values(
                        add_seconds_str(now_str(), med_cd),
                        clear_restart=False,
                    )
                )
                self.save_state()
                med_wait = await self.meditation_wait_with_concubine_recall(
                    self.state["deep_meditation_end_time"], "Meditation status"
                )
                allow_short_wait = med_wait < 60
            elif is_not_deep_meditation_response(check_resp):
                log.info("Meditation: Confirmed NOT in meditation. Restarting...")
                cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
                if self.defer_meditation_after_cultivation_cooldown(
                    "主魂", cultivation_resp, "Main meditation .闭关修炼"
                ):
                    continue
                await asyncio.sleep(3)
                med_resp = await self.send_and_wait_feedback(".深度闭关")
                med_wait = (
                    60
                    if await self.record_deep_meditation_start(
                        med_resp, "Deep meditation started"
                    )
                    else 600
                )
            elif is_deep_meditation_settlement_response(check_resp):
                log.info("Meditation time up. Settling and restarting...")
                cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
                if self.defer_meditation_after_cultivation_cooldown(
                    "主魂", cultivation_resp, "Main settlement .闭关修炼"
                ):
                    continue
                await asyncio.sleep(3)
                med_resp = await self.send_and_wait_feedback(".深度闭关")
                med_wait = (
                    60
                    if await self.record_deep_meditation_start(
                        med_resp, "Deep meditation started after settlement"
                    )
                    else 600
                )
            elif is_deep_meditation_ongoing_response(check_resp):
                if med_cd > 0:
                    log.info(f"Deep meditation in progress, remaining {med_cd}s.")
                    self.state.update(
                        self.meditation_active_state_values(
                            add_seconds_str(now_str(), med_cd),
                            clear_restart=False,
                        )
                    )
                    med_wait = await self.meditation_wait_with_concubine_recall(
                        self.state["deep_meditation_end_time"], "Meditation status"
                    )
                    allow_short_wait = med_wait < 60
                else:
                    log.info("Meditation time up. Settling and restarting...")
                    cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
                    if self.defer_meditation_after_cultivation_cooldown(
                        "主魂", cultivation_resp, "Main ongoing-zero .闭关修炼"
                    ):
                        continue
                    med_resp = await self.send_and_wait_feedback(".深度闭关")
                    med_wait = (
                        60
                        if await self.record_deep_meditation_start(
                            med_resp, "Deep meditation started after settlement"
                        )
                        else 600
                    )
                self.save_state()
            elif "冷却" in check_resp:
                if med_cd > 0:
                    log.info(f"Deep meditation on cooldown: {med_cd}s.")
                    self.state["in_deep_meditation"] = False
                    self.state["deep_meditation_end_time"] = ""
                    self.state["deep_meditation_guard_until"] = ""
                    self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), med_cd)
                    med_wait = med_cd + random.randint(10, 30)
                    self.save_state()
                else:
                    log.info("Cooldown over. Starting new meditation...")
                    resp = await self.send_and_wait_feedback(".深度闭关")
                    med_wait = (
                        60
                        if await self.record_deep_meditation_start(
                            resp, "Deep meditation started"
                        )
                        else 600
                    )
            else:
                if check_resp:
                    notify_unrecognized_response(
                        self, ".查看闭关", check_resp, log, "闭关状态"
                    )
                self.state["in_deep_meditation"] = False
                self.state["next_meditation_retry_time"] = add_seconds_str(
                    now_str(), 600
                )
                self.save_state()
                med_wait = 600

            # ---- 计算本次循环最终睡眠时间 ----
            # 考虑启阵 CD 和重试时间，取所有等待时间的最小值
            form_cd = 0
            retry_cd = 0
            if self.main_formation_enabled:
                form_cd = seconds_until(
                    add_seconds_str(
                        self.state.get("last_formation_time", now_str()), 12 * 3600
                    )
                )
                retry_cd = seconds_until(
                    self.state.get("next_formation_retry_time", "")
                )

            wait_list = [med_wait]
            if form_cd > 0:
                wait_list.append(form_cd)
            if retry_cd > 0:
                wait_list.append(retry_cd)

            final_wait = min(wait_list)
             # 移除4小时上限：让 med_wait 直接 sleep 到闭关结束
             # 保留 min(wait_list) 逻辑（取所有等待时间的最小值）
            if allow_short_wait and final_wait < 60:
                final_wait = max(5, final_wait)
            else:
                final_wait = max(60, final_wait)

            log.info(
                f"Formation/Meditation cycle complete. Next check in {int(final_wait)}s."
            )
            await asyncio.sleep(scheduler_sleep_seconds(final_wait, minimum=5))

    # ============================================================
    # 延迟强行出关
    # ============================================================

    @safe_bg_task
    async def delayed_force_exit(self, delay_sec):
        """
        在指定延迟后执行强行出关，并立即重启深度闭关。

        设计目的：
          阵法（周天星斗大阵）成功后，角色获得 6 小时的增益期。
          在增益期结束前 5 分钟（即 5h55m 后），主动退出深度闭关，
          确保增益期内可以执行其他操作（如野外历练、星辰牵引等）。
          出关后立即再次深度闭关，以继续利用剩余的增益时间。

        参数:
            delay_sec: 延迟秒数（通常为 5h55m 换算为秒）。
        """
        try:
            log.info(f"Force exit timer set for {delay_sec} seconds.")
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            if not self.is_running:
                return

            async with AtomicTaskContext(self, "MainForceExit"):
                log.info("FORCE EXIT TRIGGERED (Formation bonus window)...")
                # 强行出关是主魂操作，确保身份对齐
                await self.switch_back_to_main()
                await self.send_and_wait_feedback(".强行出关")
                self.state["in_deep_meditation"] = False
                self.state["deep_meditation_end_time"] = ""
                self.state["next_force_exit_time"] = ""
                self.save_state()
                await asyncio.sleep(10)
                # 立即重启闭关，以享受最后的加成
                resp = await self.send_and_wait_feedback(".深度闭关")
                await self.record_deep_meditation_start(
                    resp, "Deep meditation restarted after force exit"
                )
        except Exception as e:
            log.error(f"delayed_force_exit error: {e}", exc_info=True)
            # 异常时重置状态，避免闭关循环卡死
            self.state["in_deep_meditation"] = False
            self.state["next_force_exit_time"] = ""
            self.save_state()

    # ============================================================
    # 主启动方法
    # ============================================================


    # ============================================================
    # 身外化身：深度闭关辅助方法
    # ============================================================

    async def record_avatar_deep_meditation_start(self, avatar, response_text):
        """
        记录化身深度闭关的开始状态（简化版，去掉侍妾逻辑）。

        流程:
          1. 检查响应是否表示成功开启深度闭关。
          2. 如果成功，解析冷却时间（剩余秒数）。
          3. 如果直接响应中没有冷却时间，通过 .查看闭关 二次确认。
          4. 记录化身状态：in_deep_meditation=True, deep_meditation_end_time。

        返回:
            True  — 成功记录闭关状态。
            False — 未能开启深度闭关。
        """
        if not self.is_deep_meditation_start_success(response_text):
            if response_text:
                notify_unrecognized_response(
                    self, ".深度闭关", response_text, log, f"深度闭关[{avatar}]"
                )
            self.set_avatar_state(avatar, "in_deep_meditation", False)
            self.set_avatar_state(avatar, "meditation_restart_pending", True)
            return False

        cd = self.parse_wait_time(response_text)
        if cd <= 0:
            # 如果响应中没给冷却时间，通过 .查看闭关 二次确认
            verify_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
            verify_text = getattr(verify_resp, "text", "") if hasattr(verify_resp, "text") else verify_resp if isinstance(verify_resp, str) else str(verify_resp) if verify_resp else ""
            verify_cd = self.parse_wait_time(verify_text)
            if verify_cd > 0 and is_deep_meditation_ongoing_response(verify_text):
                cd = verify_cd
            else:
                log.warning(
                    f"Avatar [{avatar}] deep meditation start confirmed, but .查看闭关 "
                    f"did not return a remaining time; using 8h fallback."
                )

        self.update_avatar_states(
            avatar,
            self.meditation_active_state_values(
                add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600)
            ),
        )
        return True

    # ============================================================
    # 身外化身：助阵（化身间互相助阵）
    # ============================================================

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
        if cd > 0 and any(k in assist_text for k in ["冷却", "参与过布阵", "心神消耗", "再次启阵", "再次助阵"]):
            log.info(f"Avatar [{avatar}] assist on cooldown ({cd}s).")
            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd))
            self.set_avatar_state(avatar, "next_formation_retry_time", "")
            self.pending_formation_invite_msg = None
            return False

        # 助阵失败
        if any(k in assist_text for k in ["无法助阵", "不能助阵", "助阵失败", "已助阵"]):
            log.info(f"Avatar [{avatar}] assist failed: {assist_text[:80]}")
            return False

        log.info(f"Avatar [{avatar}] assist response: {assist_text[:80]!r}")
        return False

    async def assist_avatar_formation_invite(self, formation_msg, initiator=""):
        """实时处理本账号化身发起的启阵邀请，60 秒窗口内挑一个可用化身助阵。"""
        if not formation_msg or not hasattr(formation_msg, "id"):
            return False
        if self.formation_assist_in_progress:
            log.info(f"Formation invite {formation_msg.id} assist skipped: assist already in progress.")
            return False
        age = self.message_age_seconds(formation_msg)
        if age > 60:
            log.info(f"Formation invite {formation_msg.id} assist skipped: stale ({int(age)}s).")
            return False

        text = formation_msg.text or ""
        initiator = initiator or self.formation_invite_actor_identity(text)
        candidates = [avatar for avatar in self.avatars if avatar != initiator]
        if not candidates:
            log.info(f"Formation invite {formation_msg.id} assist skipped: no candidate avatars.")
            return False

        self.formation_assist_in_progress = True
        self.pending_formation_invite_msg = formation_msg
        try:
            log.info(
                f"Formation invite {formation_msg.id} from [{initiator or 'unknown'}]; "
                f"checking assist candidates: {', '.join(candidates)}."
            )
            for avatar in candidates:
                if self.message_age_seconds(formation_msg) > 55:
                    log.info(f"Formation invite {formation_msg.id} nearly expired; stop candidate checks.")
                    break
                if not await self.prepare_avatar_for_formation_assist(avatar):
                    continue
                if self.message_age_seconds(formation_msg) > 60:
                    log.info(f"Formation invite {formation_msg.id} expired before [{avatar}] assist.")
                    break
                assisted = await self._avatar_assist_formation(avatar)
                if assisted:
                    return True
            log.info(f"Formation invite {formation_msg.id}: no available avatar assisted.")
            return False
        finally:
            if self.pending_formation_invite_msg is formation_msg:
                self.pending_formation_invite_msg = None
            self.formation_assist_in_progress = False

    # ============================================================
    # 身外化身：延迟强行出关
    # ============================================================

    @safe_bg_task
    async def delayed_avatar_force_exit(self, avatar, delay_sec):
        """
        化身版强行出关（迁移自主循环 delayed_force_exit）。

        在指定延迟后执行强行出关，并立即重启深度闭关。
        目的：阵法增益结束前 5 分钟退出闭关，利用增益期执行操作。

        参数:
            avatar: 化身名称
            delay_sec: 延迟秒数（通常为 5h55m 换算为秒）。
        """
        log.info(f"Avatar [{avatar}] force exit timer set for {int(delay_sec)}s.")
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        if not self.is_running:
            return

        async with AtomicTaskContext(self, f"AvatarForceExit-{avatar}"):
            log.info(f"Avatar [{avatar}] FORCE EXIT TRIGGERED (Formation bonus window)...")
            # 强行出关后只走 direct deep restart，避免普通闭关循环插入 .闭关修炼。
            await self.send_and_wait_feedback_identity(avatar, ".强行出关")
            self.update_avatar_states(avatar, {
                "in_deep_meditation": False,
                "deep_meditation_end_time": "",
                "next_force_exit_time": "",
                "meditation_restart_pending": True,
                "meditation_restart_mode": "deep_only",
            })
            await asyncio.sleep(10)
            resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
            deep_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
            await self.record_avatar_deep_meditation_start(avatar, deep_text)
            log.info(f"Avatar [{avatar}] deep meditation restarted after force exit.")

    # ============================================================
    # 身外化身：启阵（迁移自主循环）
    # ============================================================

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

    def avatar_formation_block_until(self, avatar, a_state=None):
        """Return the latest active formation cooldown/retry time for an avatar."""
        a_state = a_state or self.get_avatar_state(avatar)
        future_times = []

        next_form = a_state.get("next_formation_time", "")
        if next_form and is_future(next_form):
            future_times.append(next_form)

        last_formation = a_state.get("last_formation_time", "")
        if last_formation:
            last_based_next = add_seconds_str(last_formation, 12 * 3600)
            if is_future(last_based_next):
                future_times.append(last_based_next)
                if (
                    not next_form
                    or not is_future(next_form)
                    or str_to_dt(next_form) < str_to_dt(last_based_next)
                ):
                    self.set_avatar_state(avatar, "next_formation_time", last_based_next)

        next_retry = a_state.get("next_formation_retry_time", "")
        if next_retry and is_future(next_retry):
            future_times.append(next_retry)

        if not future_times:
            return ""
        return dt_to_str(max(str_to_dt(t) for t in future_times))

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
        block_until = self.avatar_formation_block_until(avatar, a_state)
        if block_until:
            log.debug(f"Avatar [{avatar}] formation not due until {block_until}.")
            return

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

        # 情况 2：等待跨脚本助阵，等待邀请消息编辑为最终结果
        if self.is_formation_pending(resp):
            log.info(f"Avatar [{avatar}] formation pending, triggering same-account assist...")
            self.pending_formation_invite_msg = resp_msg
            asyncio.create_task(self.assist_avatar_formation_invite(resp_msg, initiator=avatar))
            updated_msg = await self.get_updated_message(resp_msg, delay_sec=75)
            updated_resp = (updated_msg.text or "") if updated_msg else ""
            if self.is_formation_success(updated_resp):
                self.pending_formation_invite_msg = None
                self.record_avatar_formation_success(avatar, self.message_effective_time_str(updated_msg))
            else:
                self.pending_formation_invite_msg = None
                log.info(f"Avatar [{avatar}] formation still pending after invite window. Retry in 10min.")
                retry_at = add_seconds_str(now_str(), 600)
                self.set_avatar_state(avatar, "next_formation_retry_time", retry_at)
            return

        # 情况 3：已有人启阵（"请勿重复操作"），本脚本分身只发起不助阵
        if "请勿重复操作" in resp or "已发布" in resp:
            retry_at = add_seconds_str(now_str(), 600)
            self.set_avatar_state(avatar, "next_formation_retry_time", retry_at)
            log.info(f"Avatar [{avatar}] formation already pending. Retry own initiation in 10min.")
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

    # ============================================================
    # 身外化身：修为不足通用处理
    # ============================================================

    async def handle_修为不足(self, avatar, retry_func, retry_args=None, retry_kwargs=None, cooldown_key="next_heart_trial_time", cooldown_hours=2):
        """
        修为不足通用处理：强行出关 → 重试 → cooldown_hours小时后再试。

        参数：
            avatar: 化身名称
            retry_func: 重试的异步函数
            retry_args: 重试函数的位置参数
            retry_kwargs: 重试函数的关键字参数
            cooldown_key: 重试失败后设置的冷却状态键名
            cooldown_hours: 重试失败后冷却小时数（默认2小时）

        返回：
            (success: bool, response: str) — success表示是否成功，response是重试后的响应
        """
        log.warning(f"Avatar [{avatar}] 修为不足, attempting force exit...")
        # 强行出关释放修为
        await self.send_and_wait_feedback_identity(
            avatar, ".强行出关", timeout=30, return_response_msg=False,
        )
        self.set_avatar_state(avatar, "in_deep_meditation", False)
        await asyncio.sleep(5)

        # 重试一次
        if retry_args is None:
            retry_args = []
        if retry_kwargs is None:
            retry_kwargs = {}
        resp = await retry_func(*retry_args, **retry_kwargs)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else str(resp) if resp else ""

        if "修为不足" in resp_text:
            # 修为仍然不足，暂停cooldown_hours小时后重试
            log.warning(f"Avatar [{avatar}] still 修为不足 after force exit, pausing {cooldown_hours}h")
            self.set_avatar_state(avatar, cooldown_key,
                                  add_seconds_str(now_str(), cooldown_hours * 3600))
            # 重新开启深度闭关
            await asyncio.sleep(3)
            await self.send_and_wait_feedback_identity(
                avatar, ".深度闭关", timeout=60, return_response_msg=False,
            )
            self.set_avatar_state(avatar, "in_deep_meditation", True)
            return False, resp_text

        # 修为足够了
        log.info(f"Avatar [{avatar}] force exit succeeded, continuing")
        return True, resp_text

    # ============================================================
    # 身外化身：共历心劫完整流程（3轮）
    # ============================================================

    def is_heart_trial_terminal_failure(self, text):
        """判断心劫流程是否已经无法继续当前锚点。"""
        if not text:
            return False
        return any(k in text for k in [
            "心劫锚点已散",
            "需重新引动天劫",
            "尚无侍妾",
            "还没有侍妾",
            "无法共历心劫",
        ])

    def is_concubine_status_panel(self, text):
        """判断文本是否是侍妾状态面板，而不是心劫轮次结果。"""
        if not text:
            return False
        return (
            any(k in text for k in ["道心侍妾", "红尘道侣", "你的侍妾"])
            and any(k in text for k in ["入梦寻图冷却", "心劫冷却", "天机代卜冷却"])
        )

    async def sync_avatar_heart_trial_cooldown_after_failure(self, avatar, reason, fallback_seconds=600):
        """心劫锚点异常后查询 .我的侍妾，按真实冷却更新化身 state。"""
        log.warning(f"Avatar [{avatar}] heart trial aborted: {reason}; syncing .我的侍妾 cooldown.")
        status_msg = await self.send_and_wait_feedback_identity(
            avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = getattr(status_msg, "text", "") if hasattr(status_msg, "text") else str(status_msg) if isinstance(status_msg, str) else ""
        if not status_text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), fallback_seconds))
            return False
        if not self.concubine_status_matches_identity(status_text, avatar):
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 30))
            return False
        if "尚无侍妾" in status_text or "还没有侍妾" in status_text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 24 * 3600))
            return True
        voyage_block_until = self.parse_concubine_voyage_status_line(status_text, avatar)
        if voyage_block_until:
            self.set_avatar_state(avatar, "next_heart_trial_time", voyage_block_until)
            return True
        clean_status = status_text.replace("**", "")
        heart_match = re.search(r"(?:共历)?心劫冷却\s*[：:]\s*([^\s\n|]+)", clean_status)
        if heart_match:
            val = heart_match.group(1).strip()
            if any(k in val for k in ["无", "可用", "可施展", "已就绪"]):
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), fallback_seconds))
                return True
            cd = self.parse_wait_time(val)
            if cd > 0:
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), cd + 60))
                return True
        cd = self.parse_wait_time(status_text)
        self.set_avatar_state(
            avatar, "next_heart_trial_time",
            add_seconds_str(now_str(), (cd + 60) if cd > 0 else fallback_seconds),
        )
        return cd > 0

    async def execute_avatar_heart_trial(self, avatar, status_msg):
        """
        化身版共历心劫完整流程。

        步骤：
        1. 回复 .共历心劫 到侍妾状态消息
        2. 等待第1轮提示
        3. 依次发送3轮 .稳（每轮等待结果确认）
        4. 结算后记录冷却

        调用前必须已经切换到目标化身身份。

        返回：
            True  — 心劫完成（3轮结束或已结算）
            False — 心劫失败（需重试或跳过）
        """
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
                return True
            # 1. 回复 .共历心劫 到侍妾状态消息
            log.info(f"Avatar [{avatar}] heart trial: replying .共历心劫 to status msg")
            trial_resp = await self.send_and_wait_feedback_identity(
                avatar, ".共历心劫", reply_to=status_msg.id,
                timeout=90, return_response_msg=True, delete_after=False,
            )
            trial_text = getattr(trial_resp, "text", "") if hasattr(trial_resp, "text") else ""
            if not trial_resp or not trial_text:
                log.warning(f"Avatar [{avatar}] heart trial: empty .共历心劫 response")
                return False

            # ---- 修为不足处理：强行出关 → 重试 → 2小时后再试 ----
            if "修为不足" in trial_text:
                # 定义重试函数
                async def retry_heart_trial():
                    status_msg_retry = await self.send_and_wait_feedback_identity(
                        avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
                    )
                    if status_msg_retry and hasattr(status_msg_retry, "id"):
                        return await self.send_and_wait_feedback_identity(
                            avatar, ".共历心劫", reply_to=status_msg_retry.id,
                            timeout=90, return_response_msg=True, delete_after=False,
                        )
                    return None

                success, trial_text = await self.handle_修为不足(avatar, retry_heart_trial, cooldown_key="next_heart_trial_time")
                if not success:
                    return False
                # 成功了，trial_text 已更新，继续下面的流程

            # 检查冷却/错误
            if any(k in trial_text for k in ["冷却", "后再", "尚未"]):
                cd = self.parse_wait_time(trial_text)
                self.set_avatar_state(avatar, "next_heart_trial_time",
                                      add_seconds_str(now_str(), cd if cd > 0 else 3600))
                log.info(f"Avatar [{avatar}] heart trial on cooldown.")
                return True  # 不算失败，只是冷却中
            if self.concubine_response_indicates_active_voyage(trial_text):
                block_until = self.concubine_voyage_block_until(avatar) or add_seconds_str(now_str(), 1800)
                self.set_avatar_state(avatar, "next_heart_trial_time", block_until)
                log.info(f"Avatar [{avatar}] heart trial blocked by active voyage until {block_until}.")
                return True

            # 检查是否要求回复目标
            if self.heart_trial_requires_reply_target(trial_text):
                log.warning(f"Avatar [{avatar}] heart trial: bot requires reply target, retrying once")
                # 重新查侍妾状态再试一次
                status_msg2 = await self.send_and_wait_feedback_identity(
                    avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
                )
                if status_msg2 and hasattr(status_msg2, "id"):
                    await asyncio.sleep(3)
                    trial_resp = await self.send_and_wait_feedback_identity(
                        avatar, ".共历心劫", reply_to=status_msg2.id,
                        timeout=90, return_response_msg=True, delete_after=False,
                    )
                    trial_text = getattr(trial_resp, "text", "") if hasattr(trial_resp, "text") else ""
                    if not trial_resp or self.heart_trial_requires_reply_target(trial_text):
                        log.warning(f"Avatar [{avatar}] heart trial: still requires reply target after retry")
                        return False

            # 检查是否是第1轮提示
            if not self.heart_trial_round_prompt(trial_text, 1):
                if "坠魔心劫" in trial_text:
                    log.info(f"Avatar [{avatar}] heart trial: got response but not round 1 prompt, proceeding anyway")
                else:
                    log.warning(f"Avatar [{avatar}] heart trial: response did not start round 1: {trial_text[:80]}")
                    return False

            # 2. 三轮心劫循环（每轮发 .稳）
            current_msg = trial_resp if hasattr(trial_resp, "id") else None
            for idx in range(1, 4):
                confirmed = False
                current_text = ""
                for attempt in range(1, 4):
                    try:
                        # 确保身份仍然正确
                        if self.current_identity != avatar:
                            log.warning(f"Avatar [{avatar}] heart trial: identity drifted to {self.current_identity}, re-switching")
                            switch_resp = await self.send_and_wait_feedback(f".切换 {avatar}", timeout=30)
                            self.current_identity = avatar
                            await asyncio.sleep(2)
                        if not current_msg or not hasattr(current_msg, "id"):
                            log.warning(f"Avatar [{avatar}] heart trial: missing round {idx} reply target.")
                            self.set_avatar_state(avatar, "next_heart_trial_time",
                                                  add_seconds_str(now_str(), 600))
                            return False

                        log.info(f"Avatar [{avatar}] heart trial: sending .稳 ({idx}/3, try {attempt}/3)")
                        await self.pause_event.wait()
                        if not await wait_for_bot_activity_before_send(self, ".稳", log):
                            return False
                        if not command_send_allowed(self, ".稳", log):
                            return False
                        remember_script_send_intent(self, ".稳")
                        sent = await self.client.send_message(
                            self.target_chat_id, ".稳",
                            reply_to=current_msg.id
                        )
                        remember_script_sent_message(self, sent)
                        record_command_sent(self, sent, ".稳", identity=avatar, source="auto", reply_to=current_msg.id, logger=log)
                        schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                        log.info(f"🟢 OUT [{avatar}]:\n.稳 ({idx}/3, try {attempt}/3)")
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] heart trial: failed to send .稳 ({idx}/3): {e}")
                        return False

                    result_msg, current_text, confirmed = await self.wait_for_heart_trial_round_result(
                        current_msg, sent, idx, timeout_sec=90, poll_sec=3,
                    )
                    if result_msg:
                        current_msg = result_msg
                        if self.heart_trial_settled(current_text):
                            log.info(f"Avatar [{avatar}] heart trial settled after round {idx}!")
                            self.set_avatar_state(avatar, "next_heart_trial_time",
                                                  add_seconds_str(now_str(), 10 * 3600))
                            return True
                        if confirmed:
                            log.info(f"Avatar [{avatar}] heart trial round {idx} confirmed")
                            break
                        if self.is_heart_trial_terminal_failure(current_text):
                            await self.sync_avatar_heart_trial_cooldown_after_failure(
                                avatar, f"round {idx} terminal response: {current_text[:80]}"
                            )
                            return False
                        if self.is_concubine_status_panel(current_text):
                            await self.sync_avatar_heart_trial_cooldown_after_failure(
                                avatar, f"round {idx} matched concubine status panel"
                            )
                            return False
                        if self.heart_trial_round_prompt(current_text, idx) and attempt < 3:
                            log.warning(f"Avatar [{avatar}] heart trial still on round {idx}; retrying.")
                            await asyncio.sleep(3)
                            continue
                        log.warning(f"Avatar [{avatar}] heart trial round {idx} not confirmed: {current_text[:80]}")
                    else:
                        log.warning(f"Avatar [{avatar}] heart trial: no result for round {idx} try {attempt}")

                    if attempt < 3:
                        await asyncio.sleep(3)

                if not confirmed:
                    log.warning(f"Avatar [{avatar}] heart trial: round {idx} failed after 3 attempts")
                    self.set_avatar_state(avatar, "next_heart_trial_time",
                                          add_seconds_str(now_str(), 600))
                    return False

            # 3轮都完成了但没看到结算消息
            log.info(f"Avatar [{avatar}] heart trial: all 3 rounds completed")
            self.set_avatar_state(avatar, "next_heart_trial_time",
                                  add_seconds_str(now_str(), 10 * 3600))
            return True

    # ============================================================
    # 身外化身：深度闭关循环（精细版）
    # ============================================================

    async def _avatar_daily_checkin(self, avatar):
        return await self.common_avatar_daily_checkin(
            avatar,
            daily_start_wait_func=seconds_until_daily_task_start,
        )

    async def run_avatar_loop(self, avatar, initial_delay=0):
        """
        化身循环（深度闭关模式，精细版）。
        按主循环 run_formation_meditation_loop 的深度闭关逻辑实现：
        - 野外历练由独立循环执行，避免被闭关/侍妾流程延迟
        - 检查深度闭关缓存状态
        - 通过 .查看闭关 确认实际状态
        - 根据响应类型（进行中/未闭关/结算/冷却/未知）分别处理
        - 深度闭关到期后自动结算并重新开启
        """
        await self.startup_done.wait()
        if initial_delay > 0:
            log.info(f"Avatar [{avatar}] meditation loop: waiting {initial_delay}s before start...")
            await asyncio.sleep(initial_delay)
        self._avatar_loop_count += 1

        while self.is_running:
            try:
                a_state = self.get_avatar_state(avatar)

                # 被动结算可能由任意指令触发；已有闭关待续时，先跳过非闭关任务。
                if not self.avatar_meditation_needs_attention(avatar):
                    # ---- 宗门点卯（每日一次，07:15 后） ----
                    await self._avatar_daily_checkin(avatar)

                if not self.avatar_meditation_needs_attention(avatar):
                    # ---- 启阵（12小时冷却，迁移自主循环） ----
                    try:
                        await self.execute_avatar_formation(avatar)
                    except Exception as e:
                        log.error(f"Avatar [{avatar}] formation error: {e}")

                # ---- 深度闭关状态管理（精细版） ----
                # 重新读取 state（野外历练可能已更新了状态）
                a_state = self.get_avatar_state(avatar)

                # 优先检查是否有重试延迟（未知响应后的冷却）
                retry_time = self.meditation_defer_until(a_state)
                if retry_time and is_future(retry_time):
                    wait_sec = min(300, seconds_until(retry_time))
                    log.info(f"Avatar [{avatar}] meditation deferred until {retry_time}.")
                    await asyncio.sleep(wait_sec)
                    continue

                if a_state.get("meditation_restart_mode") == "deep_only":
                    log.info(f"Avatar [{avatar}] direct deep meditation restart pending; sending .深度闭关.")
                    deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                    deep_text = getattr(deep_resp, "text", "") if hasattr(deep_resp, "text") else deep_resp if isinstance(deep_resp, str) else str(deep_resp) if deep_resp else ""
                    await asyncio.sleep(
                        60 if await self.record_avatar_deep_meditation_start(avatar, deep_text) else 600
                    )
                    continue

                # 检查缓存的深度闭关结束时间
                if self.ensure_meditation_guard_from_end_time(a_state):
                    self.save_state()
                guard_wait = self.meditation_guard_wait_seconds_for_state(a_state)
                med_end = a_state.get("deep_meditation_end_time", "")
                if a_state.get("meditation_restart_pending") and guard_wait <= 0:
                    med_end = ""
                    log.info(f"Avatar [{avatar}] meditation restart pending; checking immediately.")
                if guard_wait > 0:
                    # 缓存的闭关结束时间在未来，跳过 .查看闭关
                    # ⚠️ 不能直接 sleep 到闭关结束，否则野外历练会被跳过
                    # 短睡后重新循环，确保野外历练等冷却到期的功能不被阻塞
                    log.info(
                        f"Avatar [{avatar}] deep meditation protected for "
                        f"{self.compact_duration_text(guard_wait)}, short sleep then recheck."
                    )
                    med_wait = min(guard_wait, 1800)  # 最多睡 30 分钟
                else:
                    # 缓存无效或已过期，通过 .查看闭关 确认实际状态
                    log.info(f"Avatar [{avatar}] checking meditation status via .查看闭关...")
                    check_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
                    check_text = getattr(check_resp, "text", "") if hasattr(check_resp, "text") else check_resp if isinstance(check_resp, str) else str(check_resp) if check_resp else ""
                    med_cd = self.parse_wait_time(check_text)

                    med_wait = 300  # 默认等待时间

                    if med_cd > 0 and is_deep_meditation_ongoing_response(check_text):
                        # 闭关进行中，记录结束时间
                        log.info(f"Avatar [{avatar}] deep meditation in progress, remaining {med_cd}s.")
                        self.update_avatar_states(
                            avatar,
                            self.meditation_active_state_values(add_seconds_str(now_str(), med_cd)),
                        )
                        med_wait = med_cd + random.randint(10, 30)

                    elif is_not_deep_meditation_response(check_text):
                        # 未在闭关，发 .闭关修炼 + .深度闭关 重新开启
                        log.info(f"Avatar [{avatar}] confirmed NOT in meditation. Restarting...")
                        cultivation_resp = await self.send_and_wait_feedback_identity(avatar, ".闭关修炼")
                        retry_cd = self.defer_meditation_after_cultivation_cooldown(
                            avatar, self.response_text(cultivation_resp), f"[{avatar}] .闭关修炼"
                        )
                        if retry_cd:
                            med_wait = retry_cd + random.randint(10, 30)
                            continue
                        await asyncio.sleep(3)
                        deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                        deep_text = getattr(deep_resp, "text", "") if hasattr(deep_resp, "text") else deep_resp if isinstance(deep_resp, str) else str(deep_resp) if deep_resp else ""
                        med_wait = (
                            60
                            if await self.record_avatar_deep_meditation_start(avatar, deep_text)
                            else 600
                        )

                    elif is_deep_meditation_settlement_response(check_text):
                        # 结算完成，发 .闭关修炼 + .深度闭关 重新开启
                        log.info(f"Avatar [{avatar}] meditation time up. Settling and restarting...")
                        cultivation_resp = await self.send_and_wait_feedback_identity(avatar, ".闭关修炼")
                        retry_cd = self.defer_meditation_after_cultivation_cooldown(
                            avatar, self.response_text(cultivation_resp), f"[{avatar}] settlement .闭关修炼"
                        )
                        if retry_cd:
                            med_wait = retry_cd + random.randint(10, 30)
                            continue
                        await asyncio.sleep(3)
                        deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                        deep_text = getattr(deep_resp, "text", "") if hasattr(deep_resp, "text") else deep_resp if isinstance(deep_resp, str) else str(deep_resp) if deep_resp else ""
                        med_wait = (
                            60
                            if await self.record_avatar_deep_meditation_start(avatar, deep_text)
                            else 600
                        )

                    elif is_deep_meditation_ongoing_response(check_text):
                        # 闭关进行中但 med_cd == 0（时间到），结算并重新开启
                        if med_cd > 0:
                            log.info(f"Avatar [{avatar}] deep meditation in progress, remaining {med_cd}s.")
                            self.update_avatar_states(
                                avatar,
                                self.meditation_active_state_values(add_seconds_str(now_str(), med_cd)),
                            )
                            med_wait = med_cd + random.randint(10, 30)
                        else:
                            log.info(f"Avatar [{avatar}] meditation time up. Settling and restarting...")
                            cultivation_resp = await self.send_and_wait_feedback_identity(avatar, ".闭关修炼")
                            retry_cd = self.defer_meditation_after_cultivation_cooldown(
                                avatar, self.response_text(cultivation_resp), f"[{avatar}] ongoing-zero .闭关修炼"
                            )
                            if retry_cd:
                                med_wait = retry_cd + random.randint(10, 30)
                                continue
                            await asyncio.sleep(3)
                            deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                            deep_text = getattr(deep_resp, "text", "") if hasattr(deep_resp, "text") else deep_resp if isinstance(deep_resp, str) else str(deep_resp) if deep_resp else ""
                            med_wait = (
                                60
                                if await self.record_avatar_deep_meditation_start(avatar, deep_text)
                                else 600
                            )

                    elif "冷却" in check_text:
                        # 深度闭关冷却中
                        if med_cd > 0:
                            log.info(f"Avatar [{avatar}] deep meditation on cooldown: {med_cd}s.")
                            self.set_avatar_state(avatar, "in_deep_meditation", False)
                            self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                            self.set_avatar_state(avatar, "deep_meditation_guard_until", "")
                            self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), med_cd))
                            self.set_avatar_state(avatar, "meditation_restart_pending", True)
                            med_wait = med_cd + random.randint(10, 30)
                        else:
                            log.info(f"Avatar [{avatar}] cooldown over. Starting new meditation...")
                            deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                            deep_text = getattr(deep_resp, "text", "") if hasattr(deep_resp, "text") else deep_resp if isinstance(deep_resp, str) else str(deep_resp) if deep_resp else ""
                            med_wait = (
                                60
                                if await self.record_avatar_deep_meditation_start(avatar, deep_text)
                                else 600
                            )

                    else:
                        # 未知响应，10 分钟后重试
                        if check_text:
                            notify_unrecognized_response(
                                self, ".查看闭关", check_text, log, f"闭关状态[{avatar}]"
                            )
                        log.warning(f"Avatar [{avatar}] meditation unknown response: {check_text[:80]}. Retrying in 10min.")
                        self.set_avatar_state(avatar, "in_deep_meditation", False)
                        self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), 600))
                        med_wait = 600

                # ---- 侍妾批次：远航归来 -> 天机代卜 -> 入梦寻图 -> 共历心劫 -> 侍妾远航 ----
                if self.avatar_meditation_needs_attention(avatar):
                    log.info(f"Avatar [{avatar}] concubine chain skipped: meditation needs restart first.")
                else:
                    await self.execute_avatar_concubine_chain(avatar)

                # ---- 等待下次循环 ----
                # 考虑深度闭关、闭关冷却、野外历练冷却、阵法冷却、心劫冷却、入梦冷却，取最小值
                a_state = self.get_avatar_state(avatar)
                med_end2 = a_state.get("deep_meditation_end_time", "")
                next_med = a_state.get("next_meditation_time", "")
                next_ft2 = a_state.get("next_field_training_time", "")
                retry_time2 = a_state.get("next_meditation_retry_time", "")
                next_form = self.avatar_formation_block_until(avatar, a_state)
                next_force_exit = a_state.get("next_force_exit_time", "")
                next_heart2 = a_state.get("next_heart_trial_time", "")
                next_dream2 = a_state.get("next_dream_map_time", "")
                next_voyage = a_state.get("next_concubine_voyage_time", "")

                wait_candidates = [med_wait]

                def add_due_or_future(value, due_when_missing=False):
                    if value:
                        wait_candidates.append(seconds_until(value) if is_future(value) else 0)
                    elif due_when_missing:
                        wait_candidates.append(0)

                if a_state.get("last_dianmao_date") != datetime.now().strftime("%Y-%m-%d") and seconds_until_daily_task_start(datetime.now()) <= 0:
                    wait_candidates.append(60)
                add_due_or_future(med_end2)
                add_due_or_future(next_med)
                add_due_or_future(next_ft2, due_when_missing=True)
                add_due_or_future(retry_time2)
                add_due_or_future(next_form, due_when_missing=True)
                add_due_or_future(next_force_exit)
                add_due_or_future(next_heart2)
                if self.concubine_voyage_auto_start_enabled(avatar) and not self.dashboard_command_paused(".侍妾远航 冒险", avatar):
                    bound_time = self.latest_concubine_chain_time(avatar)
                    add_due_or_future(bound_time)
                else:
                    add_due_or_future(next_dream2)
                if (
                    next_voyage
                    and self.concubine_voyage_enabled(avatar)
                    and (
                        self.concubine_voyage_auto_start_enabled(avatar)
                        or a_state.get("concubine_voyage_active")
                    )
                    and not self.dashboard_command_paused(".侍妾远航 冒险", avatar)
                ):
                    add_due_or_future(next_voyage)

                final_wait = min(wait_candidates)
                 # 移除4小时上限：让 med_wait 直接 sleep 到闭关结束
                 # 保留 min(wait_candidates) 逻辑（取所有等待时间的最小值）
                final_wait = max(60, final_wait)  # 最短 60 秒

                log.info(f"Avatar [{avatar}] cycle complete. Next check in {int(final_wait)}s.")
                await asyncio.sleep(scheduler_sleep_seconds(final_wait + random.randint(10, 30), minimum=60))
                continue

            except Exception as e:
                log.error(f"Avatar [{avatar}] meditation loop error: {e}")
            finally:
                await asyncio.sleep(300)

    async def run_avatar_field_training_loop(self, avatar, initial_delay=0):
        """化身野外历练独立循环，按该化身自己的冷却时间执行。"""
        return await self.run_common_avatar_field_training_loop(
            avatar,
            initial_delay=initial_delay,
            sleep_func=scheduler_sleep_seconds,
            handle_insufficient_cultivation=True,
        )

    # ============================================================
    # 身外化身：闯塔循环（每日23点执行）
    # ============================================================

    async def run_avatar_tower_loop(self, avatar, initial_delay=0):
        """
        化身每日 23 点自动闯塔任务。
        每天 23:00 - 23:30 之间随机错开时间执行。
        """
        return await self.run_common_avatar_tower_loop(
            avatar,
            initial_delay=initial_delay,
            timeout=90,
            handle_insufficient_cultivation=True,
            require_meditation_ready=False,
            sleep_func=scheduler_sleep_seconds,
        )

    async def start(self):
        """
        脚本主入口。启动 Telethon 客户端后执行以下步骤：

        1. 登录客户端，获取自身信息。
        2. 注册消息处理器（新消息 + 编辑消息）。
        3. 执行启动同步：
           a. 观星台状态对账。
           b. 深度闭关状态对账。
           c. 阵法 CD 对账。
           d. 恢复强行出关计时器。
           e. 确保侍妾处于洞府。
           f. 释放 startup_done 信号，启动所有循环。
        4. 启动所有后台循环：
           - run_daily_tasks (每日任务)
           - run_star_gazing_loop (观星监听)
           - run_star_attraction_loop (星辰牵引)
           - run_formation_meditation_loop (阵法&闭关)
           - run_concubine_loop (侍妾管理，继承自 ConcubineMixin)
           - run_field_training_loop (野外历练，继承自 CommonCommandMixin)
           - run_sect_war_loop (宗门战)
           - run_yuanying_out_loop (元婴出窍)
           - run_rift_search_loop (探寻裂缝)
           - run_treasure_touch_loop (抚摸法宝)
        5. 保持主线程存活，直到 is_running 变为 False。
        """
        await self.client.start()
        # 热身：获取最近的对话列表，确保缓存了目标 ID
        await self.client.get_dialogs(limit=20)
        self.my_info = await self.client.get_me()
        log.info(f"Sub-Account Login: {self.my_info.first_name}")

        # 注册新消息处理器
        @self.client.on(events.NewMessage(chats=self.target_chat_id))
        async def handler(event):
            await self.handle_game_response(event)

        # 注册编辑消息处理器（主要处理万宝楼编辑和低价告警，兼探渊虚弱期检测）
        @self.client.on(events.MessageEdited(chats=self.target_chat_id))
        async def edit_handler(event):
            await log_edited_message_if_needed(self, event)
            try:
                msg = event.message
                text = msg.text or ""
                sender = await event.get_sender()
                if is_game_bot_sender(self, sender):
                    record_game_bot_activity(self, sender, log)
                    record_star_gazing_event("sub", msg, text, sender=sender, is_edited=True, logger=log)
                    self.record_star_gazing_final_report_if_needed(msg, text, source="edited message")
                    if self.maybe_record_main_yuanying_retreat_settlement_reply(msg, text, source="edited message"):
                        self.save_state()
                    # 编辑后出现元婴遁逃·虚弱 → 立刻告警并停止脚本
                    if self.is_rift_weakness_response(text) and is_edited_message_for_current_account(self, msg, text):
                        identity = tracked_command_identity_for_reply(self, msg) or getattr(self, "current_identity", "主魂")
                        log.critical(f"Rift weakness DETECTED in edited message for [{identity}].\n{text}")
                        await self.stop_for_rift_weakness(text, identity=identity, msg=msg)
                        return
                    await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                    # 编辑消息也能触发 feedback_events（bot 通过编辑回复指令，如共历心劫）
                    is_matched = match_pending_feedback_by_reply(
                        self, msg, text, self.is_loose_meditation_feedback_candidate, log, label="[EDITED-FEEDBACK]"
                    )
                    if not is_matched:
                        is_matched = match_pending_feedback_by_message_id(
                            self, msg, text, self.is_loose_meditation_feedback_candidate, log, label="[EDITED-FEEDBACK]"
                        )
                    if not is_matched:
                        is_matched = match_pending_edited_feedback(
                            self, msg, text, self.is_loose_meditation_feedback_candidate, log, id_window=30
                        )
                await self.maybe_alert_low_price_tianleizhu(msg, text, sender)
                if is_game_bot_sender(self, sender) and self.should_send_keyword_alert(msg, text):
                    await self.send_keyword_alert(msg, text, title="副号关键词提醒")
                self.maybe_record_field_training_passive(msg, text)
            except Exception as e:
                log.error(f"Edited message market alert error: {e}")

        # ---- 启动同步：副号在 15 秒后执行冷数据校验 ----
        async def startup_sync():
            try:
                # 等 15 秒让客户端稳定下来，避免启动时消息风暴
                await asyncio.sleep(15)
                log.info("Startup Sync: Smart check for stale data...")
                if self.identity_pause_seconds("主魂") > 0:
                    log.info(
                        "Startup Sync: main soul is paused; skipping main-soul startup checks "
                        "and releasing avatar loops."
                    )
                    self.startup_done.set()
                    return

                # 1. 观星台状态校验（副号主魂已迁入元婴宗，默认不再执行星宫观星台）
                if self.main_star_palace_enabled:
                    last_check = self.state.get("last_star_check_time", "")
                    next_attr = self.state.get("next_star_attraction_time", "")
                    if next_attr and is_future(next_attr):
                        log.info(
                            f"Startup Sync: Star Attraction 36h CD not ready, next at {next_attr}."
                        )
                    elif last_check and is_future(
                        add_seconds_str(last_check, STAR_CALM_INTERVAL_SECONDS)
                    ):
                        log.info("Startup Sync: Star Attraction check not ready. Skipping.")
                    else:
                        resp = await self.send_and_wait_feedback(".观星台")
                        if resp:
                            if self.star_observatory_needs_calm(resp):
                                log.info(
                                    "Startup Sync: Star flow unstable (黯淡/紊乱). Calming immediately."
                                )
                                calm_resp = await self.send_and_wait_feedback(".安抚星辰")
                                self.record_star_calm_response(calm_resp, "启动观星台安抚")
                            cd = self.parse_wait_time(resp)
                            if cd > 0:
                                self.state["last_star_check_time"] = add_seconds_str(
                                    now_str(), cd - STAR_CALM_INTERVAL_SECONDS
                                )
                                self.state["next_star_check_time"] = add_seconds_str(
                                    now_str(), cd
                                )
                            else:
                                self.state["last_star_check_time"] = now_str()
                                self.state["next_star_check_time"] = add_seconds_str(
                                    now_str(), STAR_CALM_INTERVAL_SECONDS
                                )

                # 2. 深度闭关状态对账
                old_med = self.state.get("next_deep_meditation_time", "")
                end_med = self.state.get("deep_meditation_end_time", "") or old_med

                if end_med and self.meditation_protected_until({"deep_meditation_end_time": end_med}):
                    self.state["deep_meditation_end_time"] = end_med
                    self.ensure_meditation_guard_from_end_time(self.state)
                    log.info(
                        f"Startup Sync: Local meditation time valid ({end_med}). Skipping check."
                    )
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
                        elif (
                            is_deep_meditation_settlement_response(resp_med)
                            or is_not_deep_meditation_response(resp_med)
                        ):
                            self.state["in_deep_meditation"] = False
                            self.state["deep_meditation_end_time"] = ""
                            self.state["deep_meditation_guard_until"] = ""

                # 3. 阵法 CD 对账（副号主魂不再执行星宫阵法）
                last_form = self.state.get("last_formation_time", "")
                if self.main_formation_enabled:
                    retry_form = self.state.get("next_formation_retry_time", "")
                    if retry_form and is_future(retry_form):
                        self.state["next_formation_time"] = retry_form
                        log.info(f"Startup Sync: Formation retry scheduled at {retry_form}.")
                    elif last_form:
                        self.state["next_formation_time"] = add_seconds_str(
                            last_form, 12 * 3600
                        )
                        self.state["formation_active_until"] = add_seconds_str(
                            last_form, 6 * 3600
                        )
                        if not is_future(self.state["next_formation_time"]):
                            log.info("Startup Sync: Formation ready. Waiting for main loop to trigger...")
                        else:
                            log.info(
                                f"Startup Sync: Formation on CD, next at "
                                f"{self.state['next_formation_time']}."
                            )

                    # 4. 恢复阵法强行出关计时器
                    #    脚本重启后 asyncio task 会丢，需要从状态重建
                    force_exit_at = self.state.get("next_force_exit_time", "")
                    if force_exit_at and is_future(force_exit_at):
                        force_delay = seconds_until(force_exit_at)
                        log.info(
                            f"Startup Sync: Restoring force-exit timer at {force_exit_at} "
                            f"({int(force_delay)}s)."
                        )
                        asyncio.create_task(self.delayed_force_exit(force_delay))
                    elif force_exit_at:
                        formation_active_until = (
                            add_seconds_str(last_form, 6 * 3600) if last_form else ""
                        )
                        if formation_active_until and is_future(formation_active_until):
                            log.warning(
                                f"Startup Sync: force-exit time {force_exit_at} is overdue "
                                f"but formation is still active. Triggering now."
                            )
                            self.state["next_force_exit_time"] = ""
                            self.save_state()
                            asyncio.create_task(self.delayed_force_exit(0))
                        else:
                            log.info(
                                f"Startup Sync: clearing expired force-exit time {force_exit_at}."
                            )
                            self.state["next_force_exit_time"] = ""
                    elif last_form:
                        inferred_force_exit = add_seconds_str(
                            last_form, 5 * 3600 + 55 * 60
                        )
                        if is_future(inferred_force_exit):
                            self.state["next_force_exit_time"] = inferred_force_exit
                            force_delay = seconds_until(inferred_force_exit)
                            log.info(
                                f"Startup Sync: Inferred force-exit timer at "
                                f"{inferred_force_exit} ({int(force_delay)}s)."
                            )
                            asyncio.create_task(self.delayed_force_exit(force_delay))

                # 4b. 恢复化身强行出关计时器
                for avatar_name in self.avatars:
                    a_state = self.get_avatar_state(avatar_name)
                    avatar_force_exit = a_state.get("next_force_exit_time", "")
                    if avatar_force_exit and is_future(avatar_force_exit):
                        avatar_force_delay = seconds_until(avatar_force_exit)
                        log.info(
                            f"Startup Sync: Restoring avatar [{avatar_name}] force-exit "
                            f"at {avatar_force_exit} ({int(avatar_force_delay)}s)."
                        )
                        asyncio.create_task(self.delayed_avatar_force_exit(avatar_name, avatar_force_delay))
                    elif avatar_force_exit:
                        # 过期了但增益还在，立即触发
                        avatar_active_until = a_state.get("formation_active_until", "")
                        if avatar_active_until and is_future(avatar_active_until):
                            log.warning(
                                f"Startup Sync: avatar [{avatar_name}] force-exit overdue "
                                f"but bonus active. Triggering now."
                            )
                            self.set_avatar_state(avatar_name, "next_force_exit_time", "")
                            asyncio.create_task(self.delayed_avatar_force_exit(avatar_name, 0))
                        else:
                            log.info(
                                f"Startup Sync: clearing expired avatar [{avatar_name}] "
                                f"force-exit time {avatar_force_exit}."
                            )
                            self.set_avatar_state(avatar_name, "next_force_exit_time", "")

                # 5. 确保侍妾处于洞府（仅主魂启用侍妾流程时执行）
                if self.main_concubine_enabled:
                    await self.ensure_concubine_home_default(
                        "Startup Sync default concubine placement"
                    )

                self.save_state()
                self.startup_done.set()
                log.info("Startup Sync: Finished. All loops released.")
            except Exception as e:
                log.error(f"Startup Sync FAILED: {e}", exc_info=True)
            finally:
                self.startup_done.set()  # 确保无论如何都释放循环
                log.info("Startup Sync: startup_done released (normal or fallback).")

        asyncio.create_task(startup_sync())
        asyncio.create_task(periodic_log_prune(LOG_FILE))

        # 启动所有后台循环
        asyncio.create_task(self.run_daily_tasks())           # 每日任务（点卯/闯塔/传功）
        asyncio.create_task(self.run_star_gazing_loop())      # 全天观星监听
        if self.main_star_palace_enabled:
            asyncio.create_task(self.run_star_attraction_loop())  # 星辰牵引/安抚/收集
        asyncio.create_task(self.run_formation_meditation_loop())  # 阵法 & 深度闭关
        if self.main_concubine_enabled:
            asyncio.create_task(self.run_concubine_loop())       # 侍妾管理（继承）
        asyncio.create_task(self.run_field_training_loop())   # 野外历练（继承）
        asyncio.create_task(self.run_sect_war_loop())         # 宗门战（继承）
        asyncio.create_task(self.run_custom_command_loop())    # dashboard 自定义指令
        asyncio.create_task(self.run_ask_dao_loop())           # 元婴宗问道
        asyncio.create_task(self.run_yuanying_out_loop())     # 元婴出窍
        asyncio.create_task(self.run_rift_search_loop())      # 探寻裂缝
        asyncio.create_task(self.run_fishing_loop("主魂", initial_delay=20))
        if self.main_star_palace_enabled:
            asyncio.create_task(self.run_treasure_touch_loop())   # 抚摸法宝
        # 化身闭关修炼循环（深度闭关模式，各化身错开启动避免冲突）
        for i, avatar_name in enumerate(self.avatars):
            asyncio.create_task(self.run_avatar_loop(avatar_name, initial_delay=i * 10))
            asyncio.create_task(self.run_avatar_field_training_loop(avatar_name, initial_delay=i * 10))
            asyncio.create_task(self.run_avatar_tower_loop(avatar_name, initial_delay=i * 20))
            asyncio.create_task(self.run_fishing_loop(avatar_name, initial_delay=30 + i * 10))
            if avatar_name in AVATAR_YUANYING_RIFT_AVATARS:
                asyncio.create_task(self.run_avatar_yuanying_rift_loop(avatar_name, initial_delay=i * 10))
            if avatar_name in STAR_ATTRACTION_AVATARS:
                asyncio.create_task(self.run_avatar_star_attraction_loop(avatar_name, initial_delay=i * 10))

        log.info("All Sub-Account loops started.")
        # 主线程保持存活
        while self.is_running:
            await asyncio.sleep(60)

# ============================================================
# 程序入口
# ============================================================

if __name__ == '__main__':
    c = SubCultivator()  # 创建脚本实例
    try:
        asyncio.run(c.start())  # 启动异步运行
    except KeyboardInterrupt:
        pass  # Ctrl+C 优雅退出
    except Exception as e:
        log.error(f"Main Error: {e}")
