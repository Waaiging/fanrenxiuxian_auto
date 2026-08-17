#!/usr/bin/env python3
"""
【万灵宗（小号）自动修仙脚本 v8.5】

本脚本是「凡人修仙传」Telegram 游戏的万灵宗角色自动修仙脚本。
负责自动完成万灵宗特有的灵兽玩法循环：
  1. 灵兽管理 —— 缓存灵兽列表，自动放生/寻觅/更新状态
  2. 灵兽探渊 —— Mini App 按页面冷却选择当前可用的最高战力灵兽（6h CD）
  3. 灵兽偷菜 —— 六翼优先偷取资源（4h CD）
  4. 巡边/放养/互动 —— 其他灵兽巡边，六翼完成探渊/偷菜后优先放养恢复
  5. 深度闭关 —— 自动开闭关、8小时等待、结算重开
  6. 每日任务 —— 宗门点卯
  7. 元婴出窍 —— 元婴期能力循环
  8. 探寻裂缝 —— 定时搜寻裂缝
  9. 抚摸法宝 —— 本命法宝器灵互动
  10. 关键词提醒 —— 监听群聊关键词发送通知

区分于星宫脚本 (sub_cultivator.py)：
- 仅素心子保留星宫观星/改换星移/牵引星辰；缘生子已改走太一门引道
- 增加了完整的灵兽培养放养探渊偷菜体系
- 使用 beast_lock 而非 cmd_lock 管理灵兽操作

【阅读导览】
- 常量区：灵兽优先级、巡边/放养/探渊冷却、星宫观星窗口与太一门引道冷却。
- CultivatorXiaoHao.__init__：小号主魂与三个化身的身份、宗门、锁和状态。
- 灵兽相关方法：搜索 “Beast” 或 “border patrol”，是小号最主要的业务逻辑。
- send_and_wait_feedback / send_and_wait_feedback_identity：所有指令发送和身份切换入口。
- handle_game_response：所有机器人回复统一入口，手动指令回复也会在这里同步 state。
"""
import asyncio

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
import time
import json
import os
import sys
import re
import random
import logging

# 强制设置时区为北京时间
os.environ['TZ'] = 'Asia/Shanghai'
if hasattr(time, 'tzset'):
    time.tzset()
from datetime import datetime, timedelta

from telethon import TelegramClient, events
from red_packet_features import install_red_packet_monitor
from auto_reply_features import is_auto_reply_followup, maybe_auto_reply_exchange, resume_pending_exchange_events
from automation_settings import miniapp_beast_abyss_power_in_range
from common_command_features import (
    CommonCommandMixin,
    MULAN_SUPPORT_COMMAND,
    common_command_default_state,
    seconds_until_mulan_support_start,
)
from duel_features import DuelMixin
from command_feedback import (
    _handle_telegram_send_protection,
    is_retired_auto_command,
    record_telegram_send_success,
    send_and_wait_feedback_common,
)
from concubine_features import ConcubineMixin, _ConcubineAtomicTask, concubine_default_state
from fishing_features import FishingMixin
from soul_curse_features import SoulCurseMixin
from star_gazing_collector import predicted_star_shift_dt, record_star_gazing_event
from group_visibility_control import run_telegram_write_permission_monitor
from miniapp_beast import MiniAppBeastError
from miniapp_beast_contract import MiniAppBeastContractWorker
from miniapp_beast_abyss import MiniAppBeastAbyssWorker
from miniapp_beast_seek import MiniAppBeastSeekWorker
from miniapp_daily_activities import MiniAppDailyActivities
from miniapp_fishing import MiniAppFishingAutomation
from miniapp_inventory import MiniAppInventoryWorker
from miniapp_command_routing import install_miniapp_command_router
from world_boss_features import install_world_boss_monitor
from log_utils import (
    CommandLogFilter, cap_command_retries, command_send_allowed, command_send_precheck, handle_clear_history_command, handle_anti_bot_challenge,
    handle_pause_control_command,
    is_deep_meditation_ongoing_response, is_deep_meditation_settlement_response, is_game_bot_sender,
    is_boss_monitor_alert_text,
    is_yuanying_out_settlement_response,
    is_not_deep_meditation_response, log_edited_message_if_needed, log_incoming_message,
    log_manual_outgoing_if_needed, log_mention_if_needed, mentions_self, notify_unrecognized_response,
    match_pending_edited_feedback,
    match_pending_feedback_by_message_id,
    match_pending_feedback_by_reply,
    periodic_log_prune, prune_log_file, record_bot_no_response, record_bot_response,
    record_cultivation_profile_from_text,
    record_command_sent,
    record_game_bot_activity, record_manual_command_reply_state_if_needed,
    record_message_event,
    resolve_actor_target_chats,
    actor_message_target,
    routed_telegram_event_handler,
    _chat_matches_actor_target,
    recent_profile_identity_for_text,
    remember_script_send_intent, remember_script_sent_message,
    schedule_command_auto_delete, send_text_alert, watchdog_diagnostics, is_edited_message_for_current_account, wait_for_bot_activity_before_send,
    watchdog_should_defer_for_bot_maintenance,
    watchdog_should_defer_for_active_atomic_task,
    feedback_response_conflicts,
    feedback_response_matches_command,
    feedback_response_requires_positive_match,
    is_reply_to_manual_command,
    is_reply_to_untracked_message,
    maybe_handle_han_soul_choice,
    mentions_other_user,
    mentions_other_user_for_identity,
    text_targets_current_account,
    tracked_command_identity_for_reply,
    tracked_command_text_for_reply,
)

# ============================================================
# 星宫观星与改换星移全局常量（同步自 sub_cultivator.py 原版逻辑）
# ============================================================
STAR_GAZING_INTERVAL_HOURS = 3                       # 显现间隔 3 小时
STAR_GAZING_MONITOR_LEAD_SECONDS = 3 * 60            # 提前 3 分钟开始监听
STAR_GAZING_COMMAND_LEAD_SECONDS = 60                # Good 轮次：整点前 1 分钟发送 .观星，避免改换星移回复超时
STAR_GAZING_DAILY_FALLBACK_HOUR = 23                 # 每日备用观星时间：23:59（当天未观星时的兜底）
STAR_GAZING_DAILY_FALLBACK_MINUTE = 59
STAR_GAZING_SHIFT_PROFILE = "dynamic"
STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS = (6, 28)      # 小号保留历史动态晚窗，覆盖结算较慢的轮次
STAR_GAZING_SHIFT_LEAD_SECONDS = -STAR_GAZING_SHIFT_DELAY_RANGE_SECONDS[1]  # 负数表示窗口截止在显现后
STAR_GAZING_SHIFT_GRACE_SECONDS = 1                  # 超过配置窗口 1 秒后不再补发，避免结算后无效改换
STAR_SHIFT_TARGET = "TitanCreeper"            # 分身改换星移的目标用户名
STAR_GAZING_ACTIVE_WINDOW_SECONDS = 59               # 即时模式活跃窗口为 59 秒
STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
STAR_GAZING_ROTATING_AVATARS = ["素心子"]  # 观星轮换化身列表：每次 Good 事件只派一个化身
STAR_ATTRACTION_TARGET = "天雷星"
STAR_ATTRACTION_COMMAND = f".牵引星辰 {STAR_ATTRACTION_TARGET}"
STAR_ATTRACTION_COOLDOWN_SECONDS = 36 * 3600
STAR_PRE_APPEASE_LEAD_SECONDS = 60
STAR_STATUS_RETRY_SECONDS = 10 * 60
STAR_INSUFFICIENT_RETRY_SECONDS = 60 * 60
STAR_ATTRACTION_AVATARS = set()  # 观星台/安抚/收集/牵引已迁入 miniapp，脚本不再发送
FORMATION_TARGET_INITIATORS = {
    "crayonxxin": "副号-厚土",
    "lvdoumiao": "副号-竹和生",
    "ding303": "副号-寻真子",
}
FORMATION_ASSIST_AVATARS = ["素心子"]
TAIYI_GUIDE_AVATAR = "缘生子"
CLOUD_STAIRS_AVATAR = "问心子"
TAIYI_GUIDE_COMMAND = ".引道 水"
TAIYI_GUIDE_CD_SECONDS = 12 * 3600
TAIYI_GUIDE_RETRY_SECONDS = 10 * 60
STAR_GAZING_FORBIDDEN_KEYWORDS = (
    "非星宫弟子",
    "并非星宫弟子",
    "无法施展星衍术",
)
STAR_GAZING_VALID_RESULT_KEYWORDS = (
    "【星盘显化】",
    "今日已观星一次",
    "天机不可多泄",
)


def star_gazing_shift_dt(target_dt, now=None, fate_type="", logger=None):
    """Return this account's layered shift send time."""
    return predicted_star_shift_dt(
        target_dt,
        now=now,
        fate_type=fate_type,
        logger=logger,
        shift_profile=STAR_GAZING_SHIFT_PROFILE,
    )

# =====================================================================
# 路径与常量配置
# =====================================================================
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOG_FILE = os.path.join(CONFIG_DIR, 'cultivator_xiaohao.log')
STATE_FILE = os.path.join(CONFIG_DIR, 'state_xiaohao.json')

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_SCHEDULER_SLEEP_SECONDS = 300
SCHEDULER_STALE_DUE_SECONDS = 45 * 60
SCHEDULER_STALE_STARTUP_GRACE_SECONDS = 10 * 60
HUNT_CD_SECONDS = 6 * 3600                     # 寻觅灵兽 CD 6 小时
HUNT_FULL_RETRY_SECONDS = 10 * 60              # 灵兽袋满重试 10 分钟
HUNT_FAIL_RETRY_SECONDS = 60 * 60              # 寻觅失败重试 1 小时
PASTURE_CD_SECONDS = 4 * 3600 + 60             # 历史一键放养被动同步保留
PASTURE_RETURN_DELAY_SECONDS = 60              # 放养归来后延迟
FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS = 30 * 60
BEAST_FOCUS_NAME = "六翼"
BEAST_STEAL_PREFERRED_NAME = BEAST_FOCUS_NAME
BEAST_INTERACTION_COMMAND = f".灵兽互动 {BEAST_FOCUS_NAME}"
BEAST_SOOTHE_COMMAND = f".灵兽互动 {BEAST_FOCUS_NAME} 安抚"
BEAST_INTERACTION_CD_SECONDS = 90 * 60
BEAST_CRUISE_COMMAND = f".灵兽巡游 {BEAST_FOCUS_NAME}"
BEAST_CRUISE_CD_SECONDS = 120 * 60
BEAST_BORDER_PATROL_MODES = ("斥候", "护粮", "袭营")
BEAST_BORDER_PATROL_DEFAULT_MODE = "袭营"
BEAST_BORDER_PATROL_CD_SECONDS = 75 * 60
BEAST_BORDER_PATROL_MIN_STAMINA = 24
BEAST_ACTION_RETRY_SECONDS = 10 * 60
BEAST_INJURY_DEFAULT_RETRY_SECONDS = 4 * 3600
BEAST_ABYSS_MIN_STAMINA = 30
BEAST_CRUISE_MIN_STAMINA = 20
BEAST_STEAL_MIN_STAMINA = 30
BEAST_FOCUS_PROTECT_STAMINA = 50
BEAST_ROSTER_AUTO_DAILY_LIMIT = 2
DAILY_TASK_START_HOUR = 7                      # 每日任务开始时间
DAILY_TASK_START_MINUTE = 30
SECT_SKILL_MAX_DAILY = 3                       # 宗门传功每日上限
TREASURE_TOUCH_COMMAND = ".抚摸法宝 青竹蜂云剑"
TREASURE_TOUCH_CD_SECONDS = 2 * 3600
YUANYING_OUT_CD_SECONDS = 8 * 3600
RIFT_SEARCH_CD_SECONDS = 12 * 3600
AVATAR_YUANYING_RIFT_AVATARS = {"缘生子"}           # 启用分身元婴出窍/探寻裂缝
MAIN_SOUL_WEAKNESS_PAUSE_SECONDS = 6 * 3600


# =====================================================================
# 时间工具函数
# =====================================================================

def now_str():
    return datetime.now().strftime(TIME_FORMAT)

def str_to_dt(s):
    try:
        return datetime.strptime(s, TIME_FORMAT)
    except:
        return datetime.now() - timedelta(days=1)

def dt_to_str(dt):
    return dt.strftime(TIME_FORMAT)

def add_seconds_str(s, seconds):
    dt = str_to_dt(s)
    return dt_to_str(dt + timedelta(seconds=seconds))

def is_future(s):
    if not s:
        return False
    try:
        return str_to_dt(s) > datetime.now()
    except:
        return False

def seconds_until(s):
    if not s:
        return 0
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
    target = now.replace(hour=DAILY_TASK_START_HOUR, minute=DAILY_TASK_START_MINUTE, second=0, microsecond=0)
    if now >= target:
        return 0
    return max(1, int((target - now).total_seconds()) + 1)

def daily_task_start_label():
    return f"{DAILY_TASK_START_HOUR:02d}:{DAILY_TASK_START_MINUTE:02d}"


# =====================================================================
# 日志初始化
# =====================================================================

prune_log_file(LOG_FILE)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8', mode='a'),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger('XiaoHao')
logging.getLogger('telethon').setLevel(logging.WARNING)
for handler in logging.root.handlers:
    if isinstance(handler, logging.FileHandler):
        handler.addFilter(CommandLogFilter())


def load_config():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


# =====================================================================
# AtomicTaskContext: 整体性任务独占锁上下文管理器
# =====================================================================
class AtomicTaskContext:
    """小号脚本级原子任务锁。

    用于共历心劫、星宫收集等连续指令链。灵兽流程另有 beast_lock，
    两类锁分开是为了避免灵兽长流程把普通身份任务完全堵住。
    """
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
# CultivatorXiaoHao 主类
# =====================================================================

class CultivatorXiaoHao(DuelMixin, CommonCommandMixin, ConcubineMixin, FishingMixin, SoulCurseMixin):
    """
    万灵宗小号脚本主类。
    继承 CommonCommandMixin（通用指令）和 ConcubineMixin（侍妾功能）。
    核心功能围绕灵兽展开：寻觅、放养、探渊、偷菜。
    """

    def __init__(self, session_name='xiaohao_session'):
        """初始化：加载配置、连接 Telegram、初始化状态"""
        self.account_key = "xiaohao"
        self.config_file = CONFIG_FILE
        self.config = load_config()
        self.mc = self.config.get('monitor', {})
        self.session_file = os.path.join(CONFIG_DIR, session_name)
        self.client = TelegramClient(self.session_file, self.config['api_id'], self.config['api_hash'])
        self.target_chat_id = self.mc.get('chat_id', 'fanrenxxz')
        self.topic_id = self.mc.get('topic_id')
        self.watch_bot = self.mc.get('watch_bot', 'fanrenxiuxian_bot').lower().lstrip('@')
        self.notify_users = [u.lower() for u in self.mc.get('notify_users', [])]
        self.keywords = [k.lower() for k in self.mc.get('keywords', [])]
        self.sect_name = "万灵宗"
        self.identity_sect_names = {
            "主魂": "万灵宗",
            "问心子": "凌霄宫",
            "素心子": "星宫",
            "缘生子": "太一门",
        }
        self.field_training_command = ".野外历练 谨慎"
        self.notified_alert_ids = set()
        self.telegram_write_restriction_retry_enabled = True
        self.telegram_send_protection_retry_seconds = max(
            60,
            int(self.mc.get("telegram_send_protection_retry_seconds", 15 * 60) or 15 * 60),
        )
        self.telegram_write_permission_poll_seconds = max(
            30,
            int(self.mc.get("telegram_write_permission_poll_seconds", 60) or 60),
        )

        # 运行时状态
        self.is_running = True
        self.my_info = None
        self.feedback_events = {}
        self.last_feedback_text = {}
        self.last_feedback_msg = {}
        self.feedback_commands = {}
        self.feedback_sent_ts = {}
        self.feedback_senders = {}  # msg_id → 发送指令的 sender_id（用于排除命令回声和审计）
        self.command_avatar_map = {}  # msg_id → avatar identity（用于回复归属判断）
        self.cmd_lock = asyncio.Lock()
        self.star_gazing_lock = asyncio.Lock()
        self.beast_lock = asyncio.Lock()
        # beast_lock 专门保护灵兽列表、出战/休息、探渊、偷菜、放养、巡边等状态。
        # 不和 avatar_send_lock 合并，是为了让身份指令和灵兽内部状态同步更容易排查。
        self.beast_wakeup = asyncio.Event()
        self.startup_done = asyncio.Event()
        self.pause_event = asyncio.Event()  # 暂停/恢复控制（set=运行中, clear=暂停中）
        self.pause_event.set()  # 默认运行中
        self.pause_control_event = asyncio.Event()  # 唤醒长睡眠调度器检查暂停/恢复
        # startup: check is_paused to restore paused state
        self.active_atomic_task = None       # 整体任务独占锁持有任务
        self._scheduler_task_registry = {}   # 关键后台循环名 -> asyncio.Task，供 watchdog 发现意外退出
        self.formation_assist_in_progress = False
        self._avatar_loop_count = 0          # 活跃化身循环计数（阻止主循环自动切回主魂）
        # 止/启管理员名单（只有这些人发"止"才生效）
        self.pause_admins = set(self.mc.get("pause_admins", [8325841058, -1003658665113, -1003843934428, -1003996748766]))  # 主魂(TitanCreeper)+问心子+素心子+缘生子
        self.pause_notify_user_id = 8219248252
        self._pasture_return_seen_counts = {}
        self._manual_pasture_command_ids = {}

        # ---- 身外化身系统 ----
        self.avatars = ["问心子", "素心子", "缘生子"]
        self.avatar_usernames = {
            "lianqi10000": "问心子",
            "hajiimiii": "素心子",
            "adai925": "缘生子"
        }
        self.identity_usernames = {
            "主魂": ["TitanCreeper"],
        }
        self.avatar_send_lock = asyncio.Lock()
        # 化身 chat_id 映射（供 log_utils.log_manual_outgoing_if_needed 使用）
        self._avatar_chat_ids = {
            "-1003658665113": "问心子",
            "-1003843934428": "素心子",
            "-1003996748766": "缘生子",
        }
        self._current_identity = "主魂"  # 启动时默认为主魂

        self.state_file = STATE_FILE
        self.state = self.load_state()
        self.restore_avatar_dao_names()
        # startup: restore paused state
        if self.state.get("is_paused", False):
            self.pause_event.clear()
            log.info("Startup: is_paused=True, entering paused state.")
        # Do not trust the identity persisted by the previous process.  The
        # first command must explicitly switch and confirm the live identity.
        self._persisted_identity = self.state.get("current_identity", "主魂")
        self._current_identity = ""
        self._main_confirmed = False
        self._switch_lock = asyncio.Lock()  # 防止多个任务同时发送 .切换 主魂
        self.ensure_avatar_states()
        self.migrate_yuanshengzi_taiyi_state()
        self.clear_stale_meditation_state()

    def get_identity_from_msg(self, msg):
        """优先从聊天群组获取发包/收包时的正确身份，防止被其他端串号"""
        chat_id = ""
        sender_id = ""
        if msg and hasattr(msg, "chat_id"):
            chat_id = str(msg.chat_id)
            sender_id = str(getattr(msg, "sender_id", ""))
        target_str = str(self.target_chat_id).replace("-100", "")
        if target_str in sender_id:
            return "主魂"
        elif "4240160265" in sender_id: return "无咎子"
        elif "3996748766" in sender_id: return self._avatar_chat_ids.get("-1003996748766")
        elif "3843934428" in sender_id: return self._avatar_chat_ids.get("-1003843934428")
        elif "3658665113" in sender_id: return self._avatar_chat_ids.get("-1003658665113")
        return None

    def on_avatar_dao_name_changed(self, old_name, new_name):
        global TAIYI_GUIDE_AVATAR, CLOUD_STAIRS_AVATAR
        for configured in (
            STAR_GAZING_ROTATING_AVATARS,
            FORMATION_ASSIST_AVATARS,
        ):
            configured[:] = [new_name if name == old_name else name for name in configured]
        for configured in (STAR_ATTRACTION_AVATARS, AVATAR_YUANYING_RIFT_AVATARS):
            if old_name in configured:
                configured.remove(old_name)
                configured.add(new_name)
        if TAIYI_GUIDE_AVATAR == old_name:
            TAIYI_GUIDE_AVATAR = new_name
        if CLOUD_STAIRS_AVATAR == old_name:
            CLOUD_STAIRS_AVATAR = new_name

    @property
    def current_identity(self):
        return self._current_identity

    @current_identity.setter
    def current_identity(self, value):
        if getattr(self, "_current_identity", None) != value:
            self._current_identity = value
            self.state["current_identity"] = value
            self.save_state()

    # ---- 状态管理 ----

    def load_state(self):
        """加载或初始化状态文件，确保所有必需的状态键存在"""
        default_state = {
            "date": "", "done": [], "last_pasture_time": "",
            "last_hunt_time": "", "last_steal_time": "", "last_abyss_time": "",
            "last_beast_interaction_time": "", "next_beast_interaction_time": "",
            "last_beast_cruise_time": "", "next_beast_cruise_time": "",
            "last_beast_border_patrol_time": "", "next_beast_border_patrol_time": "",
            "beast_border_patrol_name": "", "beast_border_patrol_mode": BEAST_BORDER_PATROL_DEFAULT_MODE,
            "deep_meditation_end_time": "", "deep_meditation_guard_until": "", "in_deep_meditation": False,
            "concubine_recalled_for_meditation": False, "concubine_recalled_time": "",
            "next_hunt_time": "", "next_steal_time": "", "next_abyss_time": "",
            "next_pasture_time": "", "pasture_pending_count": 0,
            "pasture_returned_count": 0, "pasture_pending_since": "",
            "next_focus_pasture_after_abyss_time": "",
            "focus_pasture_after_abyss_until": "",
            "last_pasture_return_time": "", "next_meditation_retry_time": "",
            "best_beast_injured_time": "", "next_beast_status_check_time": "",
            "best_beast_name": "", "best_beast_power": 0,
            "best_beast_status": "", "best_beast_stamina": -1, "best_beast_injury_source": "",
            "beast_hunt_stopped": False, "beast_hunt_stopped_reason": "",
            "beasts_cache": [],
            "beast_roster_updated_at": "",
            "beast_roster_auto_query_date": "", "beast_roster_auto_query_count": 0,
            "last_beast_roster_query_time": "", "last_beast_roster_query_result": "",
            "last_beast_roster_response_excerpt": "", "beast_roster_last_source": "",
            "last_treasure_touch_time": "", "next_treasure_touch_time": "",
            "last_yuanying_out_time": "", "next_yuanying_out_time": "",
            "yuanying_out_active": False, "yuanying_out_end_time": "",
            "last_rift_search_time": "", "next_rift_search_time": "",
            "level": "",
            "current_exp": None,
            "total_exp": None,
            "spirit_root": "",
            "next_switch_allowed_time": "",
            "next_formation_ban_time": "",
            "pending_star_gazing_manifest_time": "",
            "star_gazing_claimed_manifest_time": "",
            "star_gazing_claimed_avatar": "",
            "main_soul_pause_until": "",
            "main_soul_pause_reason": "",
            "is_paused": False,  # 脚本是否被暂停（"止"指令）
        }
        default_state.update(common_command_default_state())
        default_state.update(concubine_default_state())
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    s = json.load(f)
                    for k in default_state:
                        if k not in s:
                            s[k] = default_state[k]
                    # 兼容旧数据：从缓存中恢复最佳灵兽信息
                    if not s.get("best_beast_name") and s.get("beasts_cache"):
                        cache = list(s.get("beasts_cache", []))
                        cache.sort(key=lambda x: (x.get('power', 0), x.get('exp', 0), x.get('full_name', '')), reverse=True)
                        best = cache[0]
                        s["best_beast_name"] = best.get("full_name", "")
                        s["best_beast_power"] = best.get("power", 0)
                        s["best_beast_status"] = best.get("status", "未知")
                        s["best_beast_stamina"] = best.get("stamina", -1)
                    return s
            except:
                pass
        return default_state

    def save_state(self):
        """保存状态到 JSON 文件"""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Save State Error: {e}")

    # ---- 身外化身：状态管理 ----

    def ensure_avatar_states(self):
        """确保 state 中存在 avatars 分身专属状态区，每个分身独立冷却"""
        if "avatars" not in self.state:
            self.state["avatars"] = {}
        avatar_default = {
            "in_deep_meditation": False,
            "deep_meditation_end_time": "",
            "deep_meditation_guard_until": "",
            "meditation_restart_pending": False,
            "meditation_restart_mode": "",
            "next_meditation_retry_time": "",
            "next_field_training_time": "",
            "last_field_training_time": "",
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_active": False,
            "yuanying_out_end_time": "",
            "last_rift_search_time": "",
            "next_rift_search_time": "",
            "nickname": "",
            "last_tower_date": "",
            "last_mulan_support_date": "",
            "last_mulan_support_time": "",
            "next_mulan_support_time": "",
            "last_mulan_support_response": "",
            "last_mulan_support_error": "",
            "level": "",
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
            "last_taiyi_guide_time": "",
            "next_taiyi_guide_time": "",
            "last_taiyi_guide_response": "",
            "next_dream_map_time": "",
            "next_heart_trial_time": "",
            "next_divination_time": "",
            "next_concubine_voyage_time": "",
            "last_concubine_voyage_time": "",
            "concubine_voyage_active": False,
            "current_exp": 0,
            "total_exp": 0,
            "spirit_root": "",
            "last_dianmao_date": ""
        }
        nicknames = {
            "问心子": "炼气一万年",
            "素心子": "哈基米",
            "缘生子": "阿呆"
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
                if "nickname" not in self.state["avatars"][name] or self.state["avatars"][name]["nickname"] != nicknames.get(name, ""):
                    self.state["avatars"][name]["nickname"] = nicknames.get(name, "")
                    changed = True
        if changed:
            self.save_state()

    def migrate_yuanshengzi_taiyi_state(self):
        """缘生子改入太一门后，清理旧星宫排程，避免重启后误发星宫指令。"""
        avatar = TAIYI_GUIDE_AVATAR
        avatars = self.state.get("avatars") if isinstance(self.state, dict) else {}
        avatar_state = avatars.get(avatar) if isinstance(avatars, dict) else None
        if not isinstance(avatar_state, dict):
            return
        changed = False
        star_keys = (
            "next_star_attraction_time",
            "last_star_attraction_time",
            "next_star_appease_time",
            "last_star_appease_time",
            "star_pre_collect_appeased_for",
            "next_star_collect_time",
            "last_star_collect_time",
            "next_star_check_time",
            "last_star_observatory_time",
            "star_observatory_summary",
            "star_observatory_needs_refresh",
            "star_attraction_retry_time",
            "star_attraction_force_exit_tried",
            "next_star_gazing_time",
            "pending_star_gazing_target_time",
            "pending_star_gazing_date",
            "pending_star_shift_target_time",
            "next_formation_time",
            "next_formation_retry_time",
            "formation_active_until",
        )
        defaults = {
            "star_observatory_needs_refresh": False,
            "star_attraction_force_exit_tried": False,
        }
        for key in star_keys:
            if key in avatar_state and avatar_state.get(key) not in ("", False, None):
                avatar_state[key] = defaults.get(key, "")
                changed = True
        root_avatar_keys = (
            "star_gazing_assigned_avatar",
            "star_gazing_claimed_avatar",
        )
        root_owned_by_avatar = any(self.state.get(key) == avatar for key in root_avatar_keys)
        for key in root_avatar_keys:
            if self.state.get(key) == avatar:
                self.state[key] = ""
                changed = True
        root_time_keys = (
            "pending_star_gazing_manifest_time",
            "pending_star_gazing_target_time",
            "pending_star_shift_target_time",
            "star_gazing_claimed_manifest_time",
            "star_gazing_claimed_reply_msg_id",
        )
        if root_owned_by_avatar:
            root_time_keys = root_time_keys + ("next_star_gazing_time",)
            for key in root_time_keys:
                if self.state.get(key):
                    self.state[key] = ""
                    changed = True
        if changed:
            log.info(f"Startup migration: cleared old Star Palace schedule for {avatar}; Taiyi guide remains active.")
            self.save_state()

    def clear_stale_meditation_state(self):
        """清理已经过期的本地闭关标记，启动后再由 .查看闭关 对账。"""
        changed = False

        def normalize(container):
            nonlocal changed
            if self.ensure_meditation_guard_from_end_time(container):
                changed = True
            end_time = container.get("deep_meditation_end_time", "")
            if end_time and self.meditation_guard_wait_seconds_for_state(container) > 0:
                return
            if end_time and not is_future(end_time):
                container["deep_meditation_end_time"] = ""
                container["in_deep_meditation"] = False
                changed = True
            elif not end_time and container.get("in_deep_meditation"):
                container["in_deep_meditation"] = False
                changed = True

        normalize(self.state)
        for avatar_state in (self.state.get("avatars") or {}).values():
            if isinstance(avatar_state, dict):
                normalize(avatar_state)
        if changed:
            log.info("Startup cleanup: cleared stale deep meditation local state.")
            self.save_state()

    def get_avatar_state(self, avatar):
        """获取指定分身的状态字典"""
        avatar = self.resolve_avatar_identity(avatar)
        self.ensure_avatar_states()
        return self.state["avatars"].get(avatar, {})

    def set_avatar_state(self, avatar, key, value):
        """设置指定分身的状态并保存"""
        avatar = self.resolve_avatar_identity(avatar)
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar][key] = value
            self.save_state()

    def update_avatar_states(self, avatar, values):
        """批量更新分身状态，避免连续写入 state 文件。"""
        avatar = self.resolve_avatar_identity(avatar)
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar].update(values)
            self.save_state()

    def mark_avatar_meditation_restart_pending(self, avatar, source=""):
        a_state = self.get_avatar_state(avatar)
        a_state["in_deep_meditation"] = False
        a_state["deep_meditation_end_time"] = ""
        if source != "passive settlement":
            a_state["deep_meditation_guard_until"] = ""
        a_state["meditation_restart_pending"] = True
        self.save_state()
        log.info(f"Avatar [{avatar}] meditation restart pending ({source}).")

    def meditation_guard_active_for_state(self, state):
        return CommonCommandMixin.meditation_guard_active_for_state(self, state)

    def meditation_guard_wait_seconds_for_state(self, state):
        return CommonCommandMixin.meditation_guard_wait_seconds_for_state(self, state)

    def early_meditation_check_response(self, identity):
        state = self.state if identity == "主魂" else self.get_avatar_state(identity)
        return self.early_meditation_check_response_for_state(identity, state, logger=log)

    def avatar_meditation_guard_active(self, avatar):
        a_state = self.get_avatar_state(avatar)
        return self.meditation_guard_active_for_state(a_state)

    def ensure_meditation_guard_from_end_time(self, state):
        return CommonCommandMixin.ensure_meditation_guard_from_end_time(self, state)

    def avatar_meditation_needs_attention(self, avatar):
        # The guard only suppresses early meditation-maintenance checks.
        # Other avatar commands may still run while deep meditation is active.
        if self.avatar_meditation_guard_active(avatar):
            return False
        a_state = self.get_avatar_state(avatar)
        retry_time = self.meditation_defer_until(a_state)
        if retry_time and is_future(retry_time):
            return False
        if a_state.get("meditation_restart_pending"):
            return True
        if a_state.get("in_deep_meditation"):
            end_time = a_state.get("deep_meditation_end_time", "")
            return not end_time or not is_future(end_time)
        return not a_state.get("deep_meditation_end_time")

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
            
        # 检测切换回主魂（特殊文案：可能不包含“切换”二字）
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
        if not text: return
        if is_reply_to_untracked_message(self, msg): return

        if self.record_passive_concubine_voyage_response(text):
            log.info("Passive concubine voyage state synced from loose bot message.")
            return
        
        # 严格过滤：如果消息有明确的接收人但不是我，一律无视（防止同群串号）
        recent_identity = recent_profile_identity_for_text(
            self,
            text,
            msg_id=getattr(msg, "id", None),
            chat_id=getattr(msg, "chat_id", None),
        )
        avatar_marker = next((name for name in self.avatars if f"[Avatar: {name}]" in text), None)
        if not self.text_targets_self(msg, text) and not recent_identity and not avatar_marker:
            return
        
        avatar = recent_identity or avatar_marker or None
        attribution_reliable = bool(recent_identity or avatar_marker)  # 身份归属是否可靠（sender_id/reply_to/text特征命中）
        # 优先根据群组/频道 ID 强制判断发送者（防串号）
        chat_id = str(msg.chat_id)
        sender_id = str(getattr(msg, "sender_id", ""))
        target_str = str(self.target_chat_id).replace("-100", "")
        avatar_chat_ids = getattr(self, "_avatar_chat_ids", {}) or {}
        if not avatar and target_str in sender_id:
            avatar = "主魂"
            attribution_reliable = True
        elif not avatar and sender_id in avatar_chat_ids:
            avatar = avatar_chat_ids[sender_id]
            attribution_reliable = True
        elif not avatar and "4240160265" in sender_id:
            avatar = "无咎子"; attribution_reliable = True
        
        # 如果不是在专属频道发的，再退化到根据文本推测
        if not avatar:
            # 通过 reply_to 查找原始命令的发送身份（最可靠）
            reply_to = getattr(msg, 'reply_to', None)
            if reply_to:
                reply_to_id = getattr(reply_to, 'reply_to_msg_id', None) or getattr(reply_to, 'channel_post', None)
                if reply_to_id and reply_to_id in self.command_avatar_map:
                    avatar = self.command_avatar_map.get(reply_to_id)
                    attribution_reliable = True
            # fallback: 文本特征匹配
            if not avatar:
                if "[Avatar: 素心子]" in text: avatar = "素心子"; attribution_reliable = True
                elif "[Avatar: 缘生子]" in text: avatar = "缘生子"; attribution_reliable = True
                elif "[Avatar: 问心子]" in text: avatar = "问心子"; attribution_reliable = True
                elif "神念重归主魂肉身" in text or "当前操控：主魂" in text: avatar = "主魂"; attribution_reliable = True
            # 最后 fallback: current_identity（不可靠，可能是别人的回复）
            if not avatar:
                avatar = self.current_identity
                attribution_reliable = False  # fallback 不可靠，不更新境界/修为
        
        # 统一解析境界和修为，仅在归属可靠时更新（防止别人的回复污染化身境界）
        if attribution_reliable:
            record_cultivation_profile_from_text(
                self, text, identity=avatar, logger=log, source="passive profile"
            )

        main_text_targets_self = self.text_targets_self(msg, text)
        meditation_text_owned = attribution_reliable or (avatar == "主魂" and main_text_targets_self)
            
        if avatar == "主魂":
            self.save_state()

        if avatar == "主魂":
            now = now_str()
            changed = self.record_yuanying_out_active_response(text, source="passive 主魂")
            if not changed:
                changed = self.record_yuanying_out_settlement_response(text, source="passive 主魂")
            _is_force_exit = any(k in text for k in ["强行出关", "强行中断", "强行出关惩罚"])
            _is_real_exit = _is_force_exit or any(k in text for k in ["出关成功", "已出关", "闭关结束"])
            _is_deep_settlement = is_deep_meditation_settlement_response(text)
            if _is_real_exit or _is_deep_settlement:
                if not meditation_text_owned:
                    log.info("[主魂] passive: ignored unowned meditation exit text.")
                elif self.meditation_guard_active_for_state(self.state) and not _is_force_exit:
                    log.info("[主魂] passive: ignored meditation exit text while protected.")
                else:
                    self.state["in_deep_meditation"] = False
                    self.state["deep_meditation_end_time"] = ""
                    self.state["deep_meditation_guard_until"] = ""
                    self.state["next_meditation_retry_time"] = ""
                    self.state["next_meditation_time"] = ""
                    changed = True
                    log.info("Main soul passive detect closing state cleared (出关).")
            elif any(k in text for k in ["深度闭关", "开始闭关", "进入闭关", "开启闭关", "查看闭关", "预计还需"]):
                is_ongoing = any(k in text for k in ["预计还需", "还需"])
                if not meditation_text_owned:
                    log.info("[主魂] passive: ignored unowned deep meditation text.")
                elif is_not_deep_meditation_response(text) or is_deep_meditation_settlement_response(text):
                    if self.meditation_guard_active_for_state(self.state):
                        log.info("[主魂] passive: ignored meditation idle text while protected.")
                    else:
                        self.state["in_deep_meditation"] = False
                        self.state["deep_meditation_end_time"] = ""
                        self.state["deep_meditation_guard_until"] = ""
                        self.state["next_meditation_retry_time"] = ""
                        self.state["next_meditation_time"] = ""
                        changed = True
                        log.info("Main soul passive detect meditation end/idle.")
                else:
                    cd = self.parse_wait_time(text)
                    if cd > 0:
                        end_time = add_seconds_str(now, cd)
                        self.state.update(self.meditation_active_state_values(end_time, clear_restart=False))
                        changed = True
                        log.info(f"Main soul passive detect meditation active. End in {cd}s.")
            if "闭关冷却" in text or "闭关剩余" in text:
                cd = self.parse_wait_time(text, line_identifier="闭关")
                if cd > 0:
                    self.state["next_meditation_time"] = add_seconds_str(now, cd)
                    changed = True
                    log.info(f"Main soul passive meditation cooldown {cd}s.")
            if changed:
                self.save_state()
            return

        if not avatar: return
        
        now = now_str()
        a_state = self.get_avatar_state(avatar)

        # 1. 强行出关 / 明确闭关结算 → 清除深度闭关状态
        _is_force_exit = any(k in text for k in ["强行出关", "强行中断", "强行出关惩罚"])
        _is_real_exit = _is_force_exit or any(k in text for k in ["出关成功", "已出关", "闭关结束"])
        _is_deep_settlement = is_deep_meditation_settlement_response(text)
        if _is_real_exit or _is_deep_settlement:
            if not meditation_text_owned:
                log.info(f"Avatar {avatar}: ignored unowned meditation exit text.")
            elif self.meditation_guard_active_for_state(a_state) and not _is_force_exit:
                log.info(f"Avatar {avatar}: ignored meditation exit text while protected.")
            else:
                self.mark_avatar_meditation_restart_pending(avatar, "passive force exit" if _is_force_exit else "passive exit")
                log.info(f"Avatar {avatar}: passive detect closing state cleared (出关).")

        # 2. 深度闭关 / 预计还需 → 更新深度闭关结束时间
        elif any(k in text for k in ["深度闭关", "开始闭关", "进入闭关", "开启闭关", "查看闭关", "预计还需"]):
            # 如果是明确的未闭关/出关/结算文案，清除闭关状态
            is_ongoing = any(k in text for k in ["预计还需", "还需"])
            if not meditation_text_owned:
                log.info(f"Avatar {avatar}: ignored unowned deep meditation text.")
            elif is_not_deep_meditation_response(text) or is_deep_meditation_settlement_response(text):
                if self.meditation_guard_active_for_state(a_state):
                    log.info(f"Avatar {avatar}: ignored meditation idle text while protected.")
                else:
                    self.mark_avatar_meditation_restart_pending(avatar, "passive settlement")
                    log.info(f"Avatar {avatar}: passive detect Meditation end/idle.")
            else:
                cd = self.parse_wait_time(text)
                if cd > 0:
                    end_time = add_seconds_str(now, cd)
                    self.update_avatar_states(avatar, self.meditation_active_state_values(end_time))
                    log.info(f"Avatar {avatar}: passive detect Meditation active. End in {cd}s.")

        # 3. 闭关冷却中 → 更新 next_meditation_time
        if "闭关冷却" in text or "闭关剩余" in text:
            cd = self.parse_wait_time(text, line_identifier="闭关")
            if cd > 0:
                self.set_avatar_state(avatar, "next_meditation_time", add_seconds_str(now, cd))
                log.info(f"Avatar {avatar}: passive meditation cooldown {cd}s.")

        # 5. 入梦寻图
        if "当前进度：" in text and "残图" in text and "拼图" in text:
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now, 8 * 3600))
            log.info(f"Avatar {avatar}: passive detect Dream Map success. Cooldown 8h.")
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
            
        # 7. 共历心劫
        if "坠魔心劫" in text and "第一轮" in text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now, 10 * 3600))
            log.info(f"Avatar {avatar}: passive detect Heart Trial start. Cooldown 10h.")
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

        # 7.5 侍妾远航
        if self.record_concubine_voyage_response(text, identity=avatar):
            log.info(f"Avatar {avatar}: passive detect concubine voyage state.")

        # 8. 每日任务
        if "今日任务已完成" in text or "所有任务已完成" in text:
            today = datetime.now().strftime("%Y-%m-%d")
            self.set_avatar_state(avatar, "last_daily_date", today)
            log.info(f"Avatar {avatar}: passive daily tasks completed today.")

        # 9. 星宫：启阵 / 助阵
        if "成功激活了阵眼" in text or "阵法运转自如" in text:
            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now, 12 * 3600))
            log.info(f"Avatar {avatar}: passive detect formation activated, 12h cooldown.")
        elif "启阵冷却" in text or "阵法尚在运转" in text:
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now, cd))
                log.info(f"Avatar {avatar}: passive formation cooldown {cd}s.")

        # 10. 凌霄宫：登天阶 / 问心台
        if "当前登天阶冷却" in text or "云阶未散" in text:
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now, cd))
                log.info(f"Avatar {avatar}: passive cloud stairs cooldown {cd}s.")
        elif "成功登上一阶" in text or "云阶层数" in text:
            self.set_avatar_state(avatar, "next_stairs_time", now)
        if "问心台冷却" in text:
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.set_avatar_state(avatar, "next_heart_platform_time", add_seconds_str(now, cd))

        # 11. 星宫分身：观星台 / 牵引 / 安抚 / 收集。脚本和手动回复都走这里对账。
        if avatar in STAR_ATTRACTION_AVATARS:
            self.record_avatar_star_response_from_text(avatar, text, source="passive star sync")

        # 12. 太一门：缘生子引道，手动/脚本回执都同步冷却。
        if avatar == TAIYI_GUIDE_AVATAR and ("引道" in text or "太一门" in text):
            self.record_avatar_taiyi_guide_response(avatar, text, source="passive taiyi sync")

    def update_completed_weeks_from_text(self, text, source="Cloud stairs"):
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

    def get_cloud_stairs_step(self, avatar):
        """
        从缓存的化身状态中获取当前云阶步数。
        格式如 "5 / 12 阶"，提取数字部分。
        """
        current_progress = self.get_avatar_state(avatar).get("cloud_stairs_progress", "0 / 12")
        try:
            return int(re.search(r'(\d+)', current_progress).group(1))
        except Exception:
            return 0

    def is_lingxiao_unavailable_response(self, text):
        clean = str(text or "").replace("**", "")
        return "你并非凌霄宫弟子" in clean or "云阶禁制不会为你显现" in clean

    def mark_avatar_lingxiao_identity_mismatch(self, avatar, response_text="", source="Cloud stairs"):
        state = self.get_avatar_state(avatar)
        state["cloud_stairs_identity_mismatch_time"] = now_str()
        state["cloud_stairs_identity_mismatch_reason"] = str(response_text or "")[:160]
        state["next_stairs_time"] = add_seconds_str(now_str(), 120)
        state["heart_platform_date"] = ""
        state["last_heart_time"] = ""
        state["next_heart_time"] = ""
        self.current_identity = ""
        self._main_confirmed = False
        self.save_state()
        log.warning(
            f"{source} [{avatar}]: bot says non-Lingxiao disciple; "
            "invalidating cached identity and retrying after forced switch."
        )

    def record_avatar_cloud_stairs_response(self, avatar, stairs_resp, source="Cloud stairs climb"):
        if not stairs_resp:
            return False

        if self.is_lingxiao_unavailable_response(stairs_resp):
            self.mark_avatar_lingxiao_identity_mismatch(avatar, stairs_resp, source=source)
            return False

        # ---- 登阶成功 ----
        if (
            "【凌霄云阶】" in stairs_resp
            or ("踏上" in stairs_resp and "云阶" in stairs_resp)
            or ("当前云阶进度" in stairs_resp and any(k in stairs_resp for k in ["本次获得", "额外收获", "修为", "宗门贡献", "罡风淬体"]))
        ):
            self._avatar_update_cloud_stairs_progress_from_text(avatar, stairs_resp, source=source)
            self.update_completed_weeks_from_text(stairs_resp, source=source)

            now = now_str()
            self.set_avatar_state(avatar, "last_stairs_time", now)
            self.set_avatar_state(avatar, "last_stairs_success_time", now)
            self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now, 3 * 3600))
            self.record_daily_reward_event(avatar, ".登天阶", stairs_resp, source=source)
            log.info(f"Cloud stairs [{avatar}] success, next run at {self.get_avatar_state(avatar).get('next_stairs_time', '')}")
            return True

        # ---- 冷却中 ----
        cd = self.parse_wait_time(stairs_resp, line_identifier="登阶冷却")
        if cd <= 0 and any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            cd = self.parse_wait_time(stairs_resp)

        if cd > 0:
            self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now_str(), cd))
            log.info(f"Cloud stairs [{avatar}] CD from response: {cd}s, next run at {self.get_avatar_state(avatar).get('next_stairs_time', '')}")
            return False

        # ---- 不可用但无 CD 信息 ----
        if any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            log.warning(f"Cloud stairs [{avatar}] appears unavailable but no CD parsed: {stairs_resp[:100]}")
            notify_unrecognized_response(self, ".登天阶", stairs_resp, log, "登天阶冷却解析")
            self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 600))
            self.save_state()
            return False

        # ---- 完全无法识别的回复 ----
        notify_unrecognized_response(self, ".登天阶", stairs_resp, log, source)
        self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 600))
        self.save_state()
        return False

    def _avatar_update_cloud_stairs_progress_from_text(self, avatar, text: str, source="Cloud stairs"):
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
                existing_total = re.search(r'/\s*(\d+)', self.get_avatar_state(avatar).get("cloud_stairs_progress", ""))
                total = existing_total.group(1) if existing_total else "12"
    
            self.set_avatar_state(avatar, "cloud_stairs_progress", f"{current} / {total} 阶")
            log.info(f"{source}: Cloud stairs progress synced to {current}/{total}")
            return True
    
    def _avatar_restore_cloud_stairs_time_from_last(self, avatar):
            """
            从上次成功时间推断下次可登天阶时间。
            如果 next_stairs_time 为空但 last_stairs_time 存在，
            用 last_stairs_time + 3小时 推断 next_stairs_time。
    
            这样即使状态文件丢失了 next_stairs_time，也能恢复 CD 信息。
    
            返回:
                str: next_stairs_time（原始或推断的）
            """
            last_stairs = self.get_avatar_state(avatar).get("last_stairs_time", "")
            next_stairs = self.get_avatar_state(avatar).get("next_stairs_time", "")
            if next_stairs:
                return next_stairs
            if last_stairs:
                inferred = add_seconds_str(last_stairs, CLOUD_STAIRS_CD_SECONDS)
                if is_future(inferred):
                    self.set_avatar_state(avatar, "next_stairs_time", inferred)
                    self.save_state()
                    log.info(f"Cloud stairs next restored from last success: {inferred}")
                    return inferred
            return next_stairs

    def heart_platform_fallback_time(self, today):
        return f"{today} 23:50:00"

    def is_heart_platform_fallback_due(self, today):
        return datetime.now() >= str_to_dt(self.heart_platform_fallback_time(today))

    def is_avatar_heart_platform_throttled(self, avatar):
        last_heart = self.get_avatar_state(avatar).get("last_heart_time", "")
        if not last_heart:
            return False
        elapsed = (datetime.now() - str_to_dt(last_heart)).total_seconds()
        if elapsed < 3 * 3600:
            next_allowed = add_seconds_str(last_heart, 3 * 3600)
            self.set_avatar_state(avatar, "next_heart_time", next_allowed)
            log.info(f"Heart Platform [{avatar}] skipped: last use at {last_heart}, next allowed at {next_allowed}.")
            self.save_state()
            return True
        return False

    def avatar_heart_platform_already_used_today(self, avatar, today):
        if self.get_avatar_state(avatar).get("heart_platform_date") == today:
            return True
        last_heart = self.get_avatar_state(avatar).get("last_heart_time", "")
        if last_heart.startswith(today):
            self.set_avatar_state(avatar, "heart_platform_date", today)
            self.set_avatar_state(avatar, "next_heart_time", add_seconds_str(f"{today} 00:05:00", 24 * 3600))
            self.save_state()
            log.info(f"Heart Platform [{avatar}] skipped: last use already recorded today at {last_heart}.")
            return True
        return False

    def avatar_has_pending_wind_buff(self, avatar):
        last_wind = self.get_avatar_state(avatar).get("last_wind_time") or self.get_avatar_state(avatar).get("last_wind_success_time", "")
        last_stairs = self.get_avatar_state(avatar).get("last_stairs_time") or self.get_avatar_state(avatar).get("last_stairs_success_time", "")
        return bool(last_wind and (not last_stairs or last_wind > last_stairs))

    def is_avatar_nine_heaven_wind_ready(self, avatar):
        wind_cd_time = self.get_avatar_state(avatar).get("nine_heaven_wind_cd_time", 0)
        return not wind_cd_time or (isinstance(wind_cd_time, str) and not is_future(wind_cd_time))

    def record_avatar_nine_heaven_wind_response(self, avatar, wind_resp, source="Nine Heaven Wind"):
        if not wind_resp:
            return False

        if self.is_lingxiao_unavailable_response(wind_resp):
            self.mark_avatar_lingxiao_identity_mismatch(avatar, wind_resp, source=source)
            return False

        cd = self.parse_wait_time(wind_resp, line_identifier="引九天罡风")
        if cd <= 0:
            cd = self.parse_wait_time(wind_resp, line_identifier="罡风")
        if cd <= 0 and any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            cd = self.parse_wait_time(wind_resp)

        if cd > 0:
            now = datetime.now()
            self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", dt_to_str(now + timedelta(seconds=cd)))
            if cd <= 12 * 3600:
                inferred_success = now + timedelta(seconds=cd) - timedelta(seconds=12 * 3600)
                current_last = self.get_avatar_state(avatar).get("last_wind_time") or self.get_avatar_state(avatar).get("last_wind_success_time", "")
                if not current_last or inferred_success > str_to_dt(current_last):
                    inferred_str = dt_to_str(inferred_success)
                    self.set_avatar_state(avatar, "last_wind_time", inferred_str)
                    self.set_avatar_state(avatar, "last_wind_success_time", inferred_str)
                    log.info(f"{source} [{avatar}]: Wind last success inferred from CD as {inferred_str}")
            log.info(f"{source} [{avatar}]: Wind CD from response: {cd}s")
            self.save_state()
            return False

        if any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            log.warning(f"{source} [{avatar}]: Wind appears unavailable but no CD parsed: {wind_resp[:100]}")
            notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, f"{source} 冷却解析")
            self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), 600))
            self.save_state()
            return False

        if any(k in wind_resp for k in ["成功", "施展", "罡风", "淬体"]):
            cd_seconds = 12 * 3600
            now = now_str()
            self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now, cd_seconds))
            self.set_avatar_state(avatar, "last_wind_time", now)
            self.set_avatar_state(avatar, "last_wind_success_time", now)
            log.info(f"{source} [{avatar}]: Wind success, next run in {cd_seconds}s")
            self.save_state()
            return True

        log.warning(f"{source} [{avatar}]: Wind response unusual: {wind_resp[:100]}")
        notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, source)
        self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), 600))
        self.save_state()
        return False

    async def maybe_use_nine_heaven_wind(self, avatar, source="Nine Heaven Wind"):
        if self.avatar_has_pending_wind_buff(avatar):
            log.info(f"{source} [{avatar}]: pending Wind buff already exists; skip .引九天罡风.")
            return True
        if not self.is_avatar_nine_heaven_wind_ready(avatar):
            return False

        log.info(f"{source} [{avatar}]: Wind is ready. Sending .引九天罡风.")
        wind_resp = await self.send_and_wait_feedback_identity(
            avatar, ".引九天罡风", timeout=120, force_identity_check=True
        )
        wind_pending = self.record_avatar_nine_heaven_wind_response(avatar, wind_resp, source=source)
        await asyncio.sleep(3)
        return wind_pending

    async def maybe_use_heart_platform_before_climb(self, avatar, curr_step, today, allow_daily_fallback=False):
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
                return False
            # 冷却中，跳过
            if self.is_avatar_heart_platform_throttled(avatar):
                return False
            # 今天已使用，跳过
            if self.avatar_heart_platform_already_used_today(avatar, today):
                return False
    
            # 罡风 buff 还在的话，问心台让路
            wind_pending = self.avatar_has_pending_wind_buff(avatar)
            if wind_pending:
                log.info(f"Heart Platform [{avatar}] skipped: pending Wind buff has priority.")
                return False
    
            # 如果罡风冷却完毕但还没用，优先用罡风
            if self.is_avatar_nine_heaven_wind_ready(avatar):
                log.info(f"Heart Platform check at {curr_step}/12 for [{avatar}], but Wind is ready. Sending .引九天罡风 first; Heart Platform is skipped.")
                wind_pending = await self.maybe_use_nine_heaven_wind(avatar, source="Cloud Stairs")
    
                # 如果罡风用了或者还是冷却完毕状态（说明施展失败），跳过问心台
                if wind_pending or self.is_avatar_nine_heaven_wind_ready(avatar):
                    log.info(f"Heart Platform [{avatar}] skipped to preserve Wind priority.")
                    return False
    
            # 使用问心台
            reason = "late daily fallback" if late_fallback and not (8 <= curr_step <= 11) else "late cloud-stairs climb"
            log.info(f"Progress {curr_step}/12 for [{avatar}], Wind unavailable, sending .问心台 for {reason}.")
            hp_resp = await self.send_and_wait_feedback_identity(avatar, ".问心台", force_identity_check=True)
            if hp_resp:
                if self.is_lingxiao_unavailable_response(hp_resp):
                    self.mark_avatar_lingxiao_identity_mismatch(avatar, hp_resp, source="Heart Platform")
                    return False
                if any(k in hp_resp for k in ["问心台", "已经", "明天", "成功", "感受到", "感悟", "今日"]):
                    self.set_avatar_state(avatar, "heart_platform_date", today)
                    self.set_avatar_state(avatar, "last_heart_time", now_str())
                    self.set_avatar_state(avatar, "next_heart_time", add_seconds_str(f"{today} 00:05:00", 24 * 3600))
                    self.save_state()
                    log.info(f"Heart Platform [{avatar}] used/confirmed for late cloud-stairs climb.")
                    return True
                else:
                    log.warning(f"Heart Platform [{avatar}] response unusual: {hp_resp[:100]}")
                    notify_unrecognized_response(self, ".问心台", hp_resp, log, "问心台")
                    return False
            return False
    
    async def run_avatar_cloud_stairs_loop(self, avatar, initial_delay=0):
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
            self._avatar_loop_count += 1
            if initial_delay > 0:
                await asyncio.sleep(initial_delay)
            while self.is_running:
                pass  # Concurrent avatars do not wait for main identity.
                # ---- 1. 天阶状态检查 ----
                # 天阶状态只作为缓存缺失时的补账；正常登阶按固定 3 小时 CD 执行。
                next_stairs = self._avatar_restore_cloud_stairs_time_from_last(avatar)
                status_missing = not self.get_avatar_state(avatar).get("cloud_stairs_progress")
                if status_missing:
                    log.info("Checking .天阶状态 (missing cached progress)...")
                    status_resp = await self.send_and_wait_feedback_identity(avatar, ".天阶状态", force_identity_check=True)
                else:
                    log.info(f"Cloud stairs cache valid. Skipping .天阶状态. Next Stairs: {next_stairs}")
                    status_resp = None
                if status_resp:
                    if self.is_lingxiao_unavailable_response(status_resp):
                        self.mark_avatar_lingxiao_identity_mismatch(avatar, status_resp, source="Cloud stairs status")
                        await asyncio.sleep(scheduler_sleep_seconds(120))
                        continue
                    self._avatar_update_cloud_stairs_progress_from_text(avatar, status_resp, source="Cloud stairs status")
                    self.update_completed_weeks_from_text(status_resp, source="Cloud stairs status")
    
                    # 解析登阶冷却时间
                    cd = self.parse_wait_time(status_resp, line_identifier="登阶冷却")
                    if cd > 0:
                        self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now_str(), cd))
                        stairs_next = self.get_avatar_state(avatar).get("next_stairs_time", "")
                        log.info(f"Cloud stairs CD: {cd}s, next run at {stairs_next}")
                    elif "可立即登阶" in status_resp:
                        self.set_avatar_state(avatar, "next_stairs_time", "")
                        log.info("Cloud stairs ready immediately")
    
                    # 解析引九天罡风冷却时间（从天阶状态中顺带解析，减少单独查询）
                    wind_cd = self.parse_wait_time(status_resp, line_identifier="引九天罡风")
                    if wind_cd > 0:
                        self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), wind_cd))
                        wind_next = self.get_avatar_state(avatar).get("nine_heaven_wind_cd_time", "")
                        log.info(f"Nine Heaven Wind CD: {wind_cd}s, next run at {wind_next}")
                    elif "可立即施展" in status_resp or "引九天罡风" not in status_resp:
                        # 如果没有冷却时间或未解锁引九天罡风，设置为 0 表示可用
                        self.set_avatar_state(avatar, "nine_heaven_wind_cd_time", 0)
    
                    self.save_state()
    
                # ---- 2. 登天阶执行 ----
                next_time_str = self.get_avatar_state(avatar).get("next_stairs_time", "")
                curr_step = self.get_cloud_stairs_step(avatar)
                today = datetime.now().strftime('%Y-%m-%d')

                await self.maybe_use_nine_heaven_wind(avatar, source="Cloud Stairs")
    
                if not next_time_str or not is_future(next_time_str):
                    # 登阶前先考虑是否用问心台
                    await self.maybe_use_heart_platform_before_climb(avatar, curr_step, today)
    
                    stairs_resp = await self.send_and_wait_feedback_identity(avatar, ".登天阶", timeout=120, force_identity_check=True)
                    if stairs_resp:
                        self.record_avatar_cloud_stairs_response(avatar, stairs_resp)
                        self.save_state()
                    else:
                        self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 120))
                        stairs_next = self.get_avatar_state(avatar).get("next_stairs_time", "")
                        log.warning(f"Cloud stairs response missing; delaying retry until {stairs_next}.")
                        self.save_state()
                elif self.get_avatar_state(avatar).get("heart_platform_date") != today and self.is_heart_platform_fallback_due(today):
                    # 登天阶 CD 中，但问心台保底时间到了
                    await self.maybe_use_heart_platform_before_climb(avatar, curr_step, today, allow_daily_fallback=True)
    
                # ---- 3. 计算等待时间 ----
                next_stairs_str = self.get_avatar_state(avatar).get("next_stairs_time", "")
                wait_time = random.randint(10, 20)  # 如果 CD 到了，默认只睡一小会儿
    
                if next_stairs_str and is_future(next_stairs_str):
                    wait_time = seconds_until(next_stairs_str) + random.randint(5, 15)
                    log.info(f"Stairs CD active. Sleeping {wait_time}s until {next_stairs_str}")
                else:
                    log.info(f"Stairs ready or no CD. Short sleep {wait_time}s before next attempt.")
                    log.info(f"Cloud stairs next: {next_stairs_str}")
    
                # ---- 4. 问心台每日保底调度 ----
                # 如果今天还没用问心台，检查是否需要提前醒来执行保底
                if self.get_avatar_state(avatar).get("heart_platform_date") != today:
                    fallback_time = self.heart_platform_fallback_time(today)
                    if is_future(fallback_time):
                        heart_wait = seconds_until(fallback_time) + random.randint(5, 15)
                        if heart_wait < wait_time:
                            wait_time = heart_wait
                            self.set_avatar_state(avatar, "next_heart_time", fallback_time)
                            self.save_state()
                            log.info(f"Heart Platform daily fallback pending. Sleeping {wait_time}s until {fallback_time}")

                wind_cd_time = self.get_avatar_state(avatar).get("nine_heaven_wind_cd_time", "")
                if (
                    isinstance(wind_cd_time, str)
                    and is_future(wind_cd_time)
                    and not self.avatar_has_pending_wind_buff(avatar)
                ):
                    wind_wait = seconds_until(wind_cd_time) + random.randint(5, 15)
                    if wind_wait < wait_time:
                        wait_time = wind_wait
                        log.info(f"Nine Heaven Wind pending. Sleeping {wait_time}s until {wind_cd_time}")
    
                variance = random.randint(10, 30)
                log.info(f"Cloud Stairs Loop Complete. Sleep {wait_time + variance}s.")
                await asyncio.sleep(scheduler_sleep_seconds(wait_time + variance))

    # ---- 身外化身：物理串行发送管线 ----

    def _state_impending_command_wait(self, state, identity=""):
        """获取身份距离下一个待执行指令的等待时间（秒）。如果没有，返回 999999。"""
        if not isinstance(state, dict):
            return 999999
        now_dt = datetime.now()
        min_wait = 999999
        
        keys_to_check = [
            "next_meditation_retry_time",
            "next_star_palace_time",
            "next_star_gazing_time",
            "pending_star_gazing_target_time",
            "next_star_check_time",
            "next_star_appease_time",
            "next_star_collect_time",
            "next_star_attraction_time",
            "star_attraction_retry_time",
            "next_formation_time",
            "next_formation_retry_time",
            "next_force_exit_time",
            "next_dream_map_time",
            "next_divination_time",
            "next_concubine_voyage_time",
            "next_stairs_time",
            "next_heart_time",
            "next_heart_platform_time",
            "next_beast_status_check_time",
            "next_beast_border_patrol_time",
            "next_treasure_touch_time",
            "next_yuanying_out_time",
            "next_rift_search_time",
            "next_taiyi_guide_time",
            "next_switch_allowed_time",
        ]
        
        for k in keys_to_check:
            if k == "next_switch_allowed_time":
                continue
            if (
                k in ("next_yuanying_out_time", "next_rift_search_time")
                and identity in getattr(self, "avatars", [])
                and identity not in AVATAR_YUANYING_RIFT_AVATARS
            ):
                continue
            if k == "next_taiyi_guide_time" and identity != TAIYI_GUIDE_AVATAR:
                continue
            if k in ("next_heart_time", "heart_platform_time", "next_heart_platform_time"):
                today = datetime.now().strftime("%Y-%m-%d")
                if state.get("heart_platform_date") == today:
                    continue
                if hasattr(self, "is_heart_platform_fallback_due") and not self.is_heart_platform_fallback_due(today):
                    continue
            if k == "next_force_exit_time":
                active_until = state.get("formation_active_until", "")
                if not (active_until and is_future(active_until)):
                    continue
            if identity in FORMATION_ASSIST_AVATARS and k in ("next_formation_time", "next_formation_retry_time"):
                continue
            if (
                k == "next_concubine_voyage_time"
                and not self.concubine_voyage_auto_start_enabled(identity)
                and not state.get("concubine_voyage_active")
            ):
                continue
            mapped_command = self.state_time_command_for_key(k)
            if mapped_command and self.retired_auto_command_for_identity(mapped_command, identity):
                continue
            if self.state_time_command_paused(k, identity):
                continue
            t_str = state.get(k, "")
            if t_str:
                if is_future(t_str):
                    w = (str_to_dt(t_str) - now_dt).total_seconds()
                    if w < min_wait: min_wait = w
                else:
                    min_wait = 0
                    
        end_med = state.get("deep_meditation_end_time", "")
        if state.get("in_deep_meditation") and end_med:
            if is_future(end_med):
                w = (str_to_dt(end_med) - now_dt).total_seconds()
                if w < min_wait: min_wait = w
            else:
                min_wait = 0
                
        if identity == "主魂":
            support_retry = str(state.get("next_mulan_support_time") or "")
            support_due = (
                state.get("last_mulan_support_date") != datetime.now().strftime("%Y-%m-%d")
                and not self.dashboard_command_paused(self.mulan_support_command(), identity)
                and not (support_retry and is_future(support_retry))
            )
            if support_due:
                min_wait = min(
                    min_wait,
                    seconds_until_mulan_support_start(datetime.now()),
                )

        support_retry = str(state.get("next_mulan_support_time") or "")
        if (
            identity in self.avatars
            and state.get("last_mulan_support_date") != datetime.now().strftime("%Y-%m-%d")
            and not self.dashboard_command_paused(self.mulan_support_command(), identity)
            and not (support_retry and is_future(support_retry))
        ):
            min_wait = min(
                min_wait,
                seconds_until_mulan_support_start(datetime.now()),
            )

        if not state.get("in_deep_meditation") and not state.get("deep_meditation_end_time") and not state.get("next_meditation_retry_time"):
            min_wait = 0
            
        return min_wait

    def get_identity_impending_command_wait(self, identity):
        if self.identity_pause_seconds(identity) > 0:
            return 999999
        if identity == "主魂":
            wait = self._state_impending_command_wait(self.state, identity="主魂")
            return self.merge_impending_wait(wait, self.custom_command_impending_wait("主魂"))
        if identity in self.avatars:
            wait = self._state_impending_command_wait(self.get_avatar_state(identity), identity=identity)
            return self.merge_impending_wait(wait, self.custom_command_impending_wait(identity))
        return 999999

    def main_soul_pause_seconds(self):
        """Backward-compatible wrapper for old dashboard/state fields."""
        return self.identity_pause_seconds("主魂")

    def set_main_soul_pause(self, seconds, reason):
        """Backward-compatible wrapper for old dashboard/state fields."""
        return self.set_identity_pause("主魂", seconds, reason)

    async def wait_while_main_soul_paused(self, command=""):
        """Hold a main-soul command outside locks while the main soul is weak/paused."""
        last_log = 0.0
        while self.is_running:
            remaining = self.main_soul_pause_seconds()
            if remaining <= 0:
                return True
            now_mono = time.monotonic()
            if now_mono - last_log > 300:
                reason = self.state.get("main_soul_pause_reason") or "主魂暂停"
                until = self.state.get("main_soul_pause_until", "")
                log.info(
                    f"Main soul command paused ({command or 'unknown'}): {reason}; "
                    f"resume at {until}."
                )
                last_log = now_mono
            await asyncio.sleep(scheduler_sleep_seconds(remaining, minimum=5))
        return False

    async def sleep_if_main_soul_paused(self, loop_name="Main soul loop"):
        """Skip one main-soul scheduler iteration while paused, without holding locks."""
        remaining = self.main_soul_pause_seconds()
        if remaining <= 0:
            return False
        until = self.state.get("main_soul_pause_until", "")
        reason = self.state.get("main_soul_pause_reason") or "主魂暂停"
        log.info(f"{loop_name} paused: {reason}; resume at {until}.")
        await asyncio.sleep(scheduler_sleep_seconds(remaining, minimum=30))
        return True

    def get_avatar_impending_command_wait(self, avatar):
        return self.get_identity_impending_command_wait(avatar)

    def check_and_record_switch_ban(self, resp_str):
        """检测是否触发了切换的命令保护，并记录被封禁的到期时间"""
        if resp_str and any(k in resp_str for k in ["命令保护提醒", "已暂停该命令", "暂停该命令"]):
            cd = self.parse_wait_time(resp_str)
            cd_seconds = cd if cd > 0 else 3600
            ban_expire = add_seconds_str(now_str(), cd_seconds)
            self.state["next_switch_allowed_time"] = ban_expire
            self.save_state()
            log.critical(f"⚠️ Global Switch Command Banned! Suspended until {ban_expire}. Response: {resp_str[:120]}")
            return True
        return False

    def apply_switch_guard_backoff(self, command=".切换", min_seconds=60):
        """Back off all identity switching when the local command guard blocks a switch."""
        wait = self.recent_command_guard_wait(command, max_age_seconds=30)
        if wait <= 0:
            return False
        cd_seconds = max(int(min_seconds), int(wait) + 5)
        ban_expire = add_seconds_str(now_str(), cd_seconds)
        self.state["next_switch_allowed_time"] = ban_expire
        self.save_state()
        log.warning(f"Switch command guard backoff for [{command}] until {ban_expire}.")
        return True

    def command_requires_fresh_identity_confirm(self, message):
        command = str(message or "").strip().split()[0]
        return command in {".问心台", ".登天阶", ".天阶状态", ".引九天罡风"}

    async def send_and_wait_feedback_identity(self, identity, message, timeout=45, max_retries=2, force_identity_check=False, **kwargs):
        """
        带身份感知的物理串行发送管线。
        """
        identity = self.resolve_avatar_identity(identity)
        if is_retired_auto_command(message, actor=self, identity=identity):
            log.info("Retired auto command blocked before identity alignment: %s", str(message or "").strip())
            return None
        force_meditation_check = bool(kwargs.pop("force_meditation_check", False))
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        if str(message or "").strip() == ".查看闭关" and identity in self.avatars and not force_meditation_check:
            guarded_resp = self.early_meditation_check_response(identity)
            if guarded_resp:
                return guarded_resp

        # 暂停守卫：等待恢复信号
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused(identity, message):
            return None
        if not command_send_precheck(self, message, log, identity=identity):
            return None
        high_priority_identity_command = self.time_critical_identity_command(message)
        allow_unconfirmed_switch = str(message).startswith(".改换星移")
        force_fresh_identity_confirm = (
            bool(force_identity_check)
            and identity in self.avatars
            and self.command_requires_fresh_identity_confirm(message)
        )

        _t0 = time.monotonic()
        log.info(f"[DEBUG-IDENTITY] [{identity}] ENTER send_and_wait_feedback_identity, cmd={message!r}, lock_held={self.avatar_send_lock.locked()}")
        yield_attempts = 0
        defer_started_at = None
        urgent_yield_attempts = 0
        urgent_defer_started_at = None
        while True:
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
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

                if self.should_wait_for_atomic_task(message):
                    should_yield = True
                    wait_sec_to_sleep = 0.5
                elif self.current_identity and self.current_identity != identity:
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
                                log.info(f"Avatar switch deferred: {self.current_identity} has commands due in {wait_sec:.1f}s. [{identity}] yields lock.")
                            yield_attempts += 1
                            should_yield = True
                            wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))
                        else:
                            log.info(
                                f"Avatar switch to {identity} proceeds after {deferred_for:.1f}s defer; "
                                f"{self.current_identity} still reports due commands."
                            )
                
                if not should_yield:
                    needs_switch = self.current_identity != identity or force_fresh_identity_confirm
                    if needs_switch:
                        ban_time = self.state.get("next_switch_allowed_time", "")
                        if ban_time and is_future(ban_time):
                            log.warning(f"🚫 Global switch is banned until {ban_time}. Blocking switch to {identity}.")
                            return None
                        switch_target = "主魂" if identity == "主魂" else identity
                        switch_cmd = f".切换 {switch_target}"
                        if self.current_identity == identity and force_fresh_identity_confirm:
                            log.info(f"🔄 Avatar switch: fresh-confirming {identity} before {message} (sending {switch_cmd})")
                        else:
                            log.info(f"🔄 Avatar switch: {self.current_identity} → {identity} (sending {switch_cmd})")
                        log.info(f"[DEBUG-IDENTITY] [{identity}] sending switch cmd: {switch_cmd}")
                        switch_resp = await self._send_and_wait_feedback_raw(
                            switch_cmd,
                            timeout=8 if high_priority_identity_command else 30,
                            max_retries=1 if high_priority_identity_command else 2,
                            suppress_no_response_alert=high_priority_identity_command,
                            skip_bot_activity_wait=True,
                        )
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        log.info(f"[DEBUG-IDENTITY] [{identity}] switch response: {resp_str[:120]!r}")
                        if not resp_str and self.apply_switch_guard_backoff(switch_cmd):
                            log.error(f"❌ Switch to {identity} blocked by command guard. Blocking subsequent command: {message}")
                            return None
                        if self.check_and_record_switch_ban(resp_str):
                            log.error(f"❌ Switch to {identity} failed due to global command ban. Blocking subsequent command: {message}")
                            return None
                        is_success = False
                        if self.current_identity == identity and not force_fresh_identity_confirm:
                            is_success = True
                            log.info(f"✅ Avatar switch passively confirmed: now {identity}")
                        if resp_str and any(k in resp_str for k in ["成功", "已切换", "当前操控", identity]):
                            is_success = True
                        if is_success:
                            self.current_identity = identity
                            self._main_confirmed = False  # 化身已切换，主魂确认失效
                            log.info(f"✅ Avatar switch confirmed: now {identity}")
                        elif allow_unconfirmed_switch:
                            log.warning(
                                f"Avatar switch to {identity} has no confirmed feedback; "
                                f"continuing for high-priority {message}."
                            )
                            self.current_identity = identity
                            self._main_confirmed = False
                        else:
                            log.error(f"❌ Avatar switch to {identity} FAILED! Blocking subsequent command: {message}. Response: {resp_str[:120]}")
                            return None
                        switched_this_iteration = True
                        log.info(f"✅ Avatar switch ready: now {identity}; sending pending command immediately.")
                    else:
                        log.info(f"[DEBUG-IDENTITY] [{identity}] already in correct identity, skip switch")

                    if switched_this_iteration:
                        critical_wait = -1
                    else:
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
                    log.info(f"[DEBUG-IDENTITY] [{identity}] sending cmd: {message!r}")
                    resp = await self._send_and_wait_feedback_raw(
                        message, timeout=timeout, max_retries=max_retries, skip_bot_activity_wait=True, **kwargs
                    )
                    resp_preview = (getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else "")[:80]
                    log.info(f"[DEBUG-IDENTITY] [{identity}] cmd response: {resp_preview!r}")

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
                log.info(f"[DEBUG-IDENTITY] [{identity}] yielding lock, sleeping {wait_sec_to_sleep:.1f}s")
                await asyncio.sleep(wait_sec_to_sleep)

    async def switch_back_to_main(self, force=False):
        """
        兼容旧调用的主魂切换钩子。

        默认不主动切回主魂；真正需要发送主魂指令时，send_and_wait_feedback()
        会在指令预检通过后再对齐身份，避免流程收尾阶段产生无后续指令的空切换。
        """
        # 化身正在发送命令时跳过
        if self.avatar_send_lock.locked():
            return
        if self._main_confirmed or self.current_identity == "主魂":
            return
        if not force:
            return
        if self.identity_pause_seconds("主魂") > 0:
            log.info("switch_back_to_main skipped: main soul is paused.")
            return
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task():
            await asyncio.sleep(0.5)

        if self._main_confirmed:
            return  # 已确认在主魂，跳过
        async with self._switch_lock:
            if self._main_confirmed:
                return  # 加锁后再检查——其他任务可能已完成切换
            yield_attempts = 0
            while True:
                should_yield = False
                wait_sec_to_sleep = 0
                if not await wait_for_bot_activity_before_send(self, ".切换 主魂", log):
                    return
                async with self.avatar_send_lock:
                    if self.should_wait_for_atomic_task():
                        should_yield = True
                        wait_sec_to_sleep = 0.5
                    elif self.current_identity and self.current_identity != "主魂" and self.current_identity in self.avatars:
                        wait_sec = self.get_avatar_impending_command_wait(self.current_identity)
                        if 0 <= wait_sec <= 60:
                            log.info(f"switch_back_to_main deferred: {self.current_identity} has commands due in {wait_sec:.1f}s.")
                            return
                    
                    if not should_yield:
                        ban_time = self.state.get("next_switch_allowed_time", "")
                        if ban_time and is_future(ban_time):
                            log.warning(f"🚫 Global switch is banned until {ban_time}. Blocking switch_back_to_main.")
                            return
                        try:
                            # ⚠️ 必须用 _send_and_wait_feedback_raw，因为外层已持有 avatar_send_lock
                            # 用 send_and_wait_feedback 会再次获取同一把锁 → 死锁
                            resp = await self._send_and_wait_feedback_raw(
                                ".切换 主魂", timeout=10, max_retries=0, skip_bot_activity_wait=True
                            )
                            resp_str = str(resp) if resp else ""
                            if any(k in resp_str for k in ["成功", "已切换", "主魂", "当前操控"]):
                                log.info(f"switch_back_to_main: confirmed ({self.current_identity} -> 主魂).")
                                self._main_confirmed = True
                            elif self.apply_switch_guard_backoff(".切换 主魂"):
                                log.warning("switch_back_to_main: command guard blocked switch, keeping current identity unconfirmed.")
                                return
                            else:
                                log.warning(f"switch_back_to_main: unconfirmed: {resp_str[:80]}, forcing reset.")
                        except Exception as e:
                            log.error(f"switch_back_to_main failed: {e}, forcing identity reset.")
                        self.current_identity = "主魂"
                        return

                if should_yield:
                    await asyncio.sleep(wait_sec_to_sleep)

    # ---- 指令发送 ----

    def reserve_beast_roster_auto_query(self, hold_for_send=True):
        """Reserve one automatic roster query before entering the send pipeline."""
        if self.normalize_beast_roster_auto_query_quota():
            self.save_state()
        if self.beast_roster_auto_query_remaining() <= 0:
            self.defer_beast_roster_auto_query_after_limit()
            return None
        used = self.record_beast_roster_auto_query_sent()
        if hold_for_send:
            self._beast_roster_query_reservations = int(
                getattr(self, "_beast_roster_query_reservations", 0) or 0
            ) + 1
        self.save_state()
        return used

    def before_auto_command_send(self, message):
        """Enforce the small-account daily cap at the actual send boundary."""
        if str(message or "").strip() != ".我的灵兽":
            return True

        reservations = int(getattr(self, "_beast_roster_query_reservations", 0) or 0)
        if reservations > 0:
            self._beast_roster_query_reservations = reservations - 1
            return True

        used = self.reserve_beast_roster_auto_query(hold_for_send=False)
        if used is None:
            log.warning("Blocked automatic .我的灵兽: daily cap reached.")
            return False
        log.info(
            f"Automatic .我的灵兽 reserved at send boundary "
            f"({used}/{BEAST_ROSTER_AUTO_DAILY_LIMIT} today)."
        )
        return True

    async def run_telegram_write_permission_monitor(self):
        await run_telegram_write_permission_monitor(
            self,
            log,
            protection_handler=_handle_telegram_send_protection,
        )

    async def send_to_game(self, message, reply_to=None):
        """发送指令到游戏群组（带活跃度检测和守卫）"""
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()
        if getattr(self, "current_identity", "主魂") == "主魂":
            if not await self.wait_while_identity_paused("主魂", message):
                return None

        try:
            target_chat, target_reply = actor_message_target(self, reply_to=reply_to)
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
            if not command_send_allowed(self, message, log):
                return None
            if not self.before_auto_command_send(message):
                return None
            remember_script_send_intent(self, message)
            msg = await self.client.send_message(target_chat, message, reply_to=target_reply)
            await record_telegram_send_success(self, logger=log)
            remember_script_sent_message(self, msg)
            # 记录 msg_id → avatar，供回复归属判断
            self.command_avatar_map[msg.id] = self.current_identity
            record_command_sent(self, msg, message, identity=getattr(self, "current_identity", "主魂"), source="auto", reply_to=target_reply, logger=log)
            schedule_command_auto_delete(self, msg, text=message, logger=log)
            log.info(f"🟢 OUT [{self.current_identity}]:\n{message}")
            return msg.id
        except Exception as e:
            log.error(f"Send Error [{message}] reply_to={target_reply}: {e}")
            await _handle_telegram_send_protection(
                self, message, e, logger=log, identity=getattr(self, "current_identity", "主魂")
            )
            return None

    async def send_and_wait_feedback(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, force_identity_check=False, suppress_no_response_alert=False, force_meditation_check=False):
        """
        发送指令并等待回复（带 avatar_send_lock 保护）。
        所有主魂业务通过此方法发送。如果当前身份不是主魂，自动切回主魂再发送。
        """
        if is_retired_auto_command(message, actor=self, identity="主魂"):
            log.info("Retired auto command blocked before identity alignment: %s", str(message or "").strip())
            return None
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.should_wait_for_atomic_task(message):
            await asyncio.sleep(0.5)

        if str(message or "").strip() == ".查看闭关" and not force_meditation_check:
            guarded_resp = self.early_meditation_check_response("主魂")
            if guarded_resp:
                return guarded_resp

        # 暂停守卫：等待恢复信号
        await self.pause_event.wait()
        if not await self.wait_while_identity_paused("主魂", message):
            return None

        yield_attempts = 0
        defer_started_at = None
        urgent_yield_attempts = 0
        urgent_defer_started_at = None
        while True:
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
            should_yield = False
            wait_sec_to_sleep = 0
            switched_this_iteration = False
            async with self.avatar_send_lock:
                # 主魂自动身份对齐：如果当前是分身身份，或者主魂未确认，先切回主魂
                # avatar_send_lock 已防止并发冲突，化身下次 send_and_wait_feedback_identity 会自行切回
                if self.should_wait_for_atomic_task(message):
                    should_yield = True
                    wait_sec_to_sleep = 0.5
                elif self.current_identity != "主魂" or not self._main_confirmed:
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
                        # 检查全局切换封禁是否在冷却中
                        ban_time = self.state.get("next_switch_allowed_time", "")
                        if ban_time and is_future(ban_time):
                            log.warning(f"🚫 Global switch is banned until {ban_time}. Blocking auto-switch back to 主魂.")
                            return None

                        log.info(f"🔄 Auto switch back to 主魂 from {self.current_identity} (before main command)")
                        switch_resp = await self._send_and_wait_feedback_raw(
                            ".切换 主魂", timeout=30, max_retries=2, skip_bot_activity_wait=True
                        )
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        
                        if not resp_str and self.apply_switch_guard_backoff(".切换 主魂"):
                            log.error(f"❌ Auto-switch back to 主魂 blocked by command guard. Blocking main command: {message}")
                            return None

                        # 检测是否被封禁
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
                        skip_bot_activity_wait=True,
                    )
                    # 主魂境界由 maybe_record_avatar_passive_states 统一更新（有归属校验，防污染）
                    return resp

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

    async def _send_and_wait_feedback_raw(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, suppress_no_response_alert=False, skip_bot_activity_wait=False):
        """内部发送方法（不获取 avatar_send_lock，已被外部调用方持有）"""
        try:
            return await send_and_wait_feedback_common(
                self, log, message, timeout=timeout, max_retries=max_retries,
                reply_to=reply_to, return_msg=return_msg, return_response_msg=return_response_msg,
                delete_after=delete_after, return_msg_role="sent",
                suppress_no_response_alert=suppress_no_response_alert,
                skip_bot_activity_wait=skip_bot_activity_wait,
            )
        except Exception as e:
            log.error(f"_send_and_wait_feedback_raw [{message[:40]}] crashed: {e}")
            return None

    # ---- 每日任务 ----

    async def run_daily_support_tasks(self):
        """每天 10:00 后执行仍有效的慕兰支援。"""
        return await self.run_common_mulan_support_loop(
            pre_loop_func=lambda: self.sleep_if_main_soul_paused("Daily tasks"),
            sleep_func=scheduler_sleep_seconds,
        )

    def _stale_fishing_active_identities(self, overdue_seconds=60):
        return []
        stale = []
        for identity in ["主魂", *list(getattr(self, "avatars", []) or [])]:
            try:
                state = self.get_fishing_state(identity)
            except Exception:
                continue
            due_at = str(state.get("active_due_at") or "").strip()
            if not state.get("active") or not due_at or is_future(due_at):
                continue
            overdue = int((datetime.now() - str_to_dt(due_at)).total_seconds())
            if overdue >= abs(int(overdue_seconds)):
                stale.append((identity, due_at, overdue))
        return stale

    async def run_health_watchdog_loop(self):
        await self.startup_done.wait()
        lock_started_at = None
        stale_due_watch_started_at = time.monotonic()
        while getattr(self, "is_running", True):
            try:
                stale_fishing = self._stale_fishing_active_identities()
                if stale_fishing:
                    detail = ", ".join(
                        f"{identity} due {due_at} ({overdue}s overdue)"
                        for identity, due_at, overdue in stale_fishing
                    )
                    log.critical(f"Xiaohao watchdog: stale fishing active detected: {detail}; restarting process.")
                    self.save_state()
                    os.execv(sys.executable, [sys.executable, *sys.argv])

                dead_tasks = self.dead_scheduler_tasks()
                if dead_tasks:
                    detail = ", ".join(dead_tasks)
                    log.critical(f"Xiaohao watchdog: scheduler task stopped ({detail}); restarting process.")
                    self.save_state()
                    os.execv(sys.executable, [sys.executable, *sys.argv])

                if time.monotonic() - stale_due_watch_started_at >= SCHEDULER_STALE_STARTUP_GRACE_SECONDS:
                    stale_due = self.stale_scheduler_due_items()
                    if stale_due:
                        detail = ", ".join(
                            f"{key}/{command} due {due_at} ({overdue}s overdue)"
                            for key, command, due_at, overdue in stale_due
                        )
                        if watchdog_should_defer_for_active_atomic_task(
                            self, log, reason=f"Xiaohao watchdog stale due ({detail})"
                        ):
                            stale_due_watch_started_at = time.monotonic()
                        elif watchdog_should_defer_for_bot_maintenance(
                            self, log, reason=f"Xiaohao watchdog stale due ({detail})"
                        ):
                            stale_due_watch_started_at = time.monotonic()
                        else:
                            log.critical(
                                f"Xiaohao watchdog: scheduler due item stale: {detail}; "
                                f"diagnostics: {watchdog_diagnostics(self)}; restarting process."
                            )
                            self.save_state()
                            os.execv(sys.executable, [sys.executable, *sys.argv])

                if self.avatar_send_lock.locked():
                    if lock_started_at is None:
                        lock_started_at = time.monotonic()
                    held_for = time.monotonic() - lock_started_at
                    if held_for >= 10 * 60:
                        if watchdog_should_defer_for_bot_maintenance(
                            self, log, reason=f"Xiaohao watchdog avatar_send_lock held {held_for:.0f}s"
                        ):
                            lock_started_at = time.monotonic()
                        else:
                            log.critical(
                                f"Xiaohao watchdog: avatar_send_lock held for {held_for:.0f}s; "
                                f"diagnostics: {watchdog_diagnostics(self)}; restarting process."
                            )
                            self.save_state()
                            os.execv(sys.executable, [sys.executable, *sys.argv])
                else:
                    lock_started_at = None
            except Exception as exc:
                log.error(f"Xiaohao watchdog loop error: {exc}", exc_info=True)
            await asyncio.sleep(60)

    # ---- 指令解析工具 ----

    def parse_wait_time(self, text, find_min=False, line_identifier=None):
        if not text:
            return -1
        # 先清理所有 markdown 加粗标记和空格
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
        if not timed_list:
            return -1
        return min(timed_list) if find_min else timed_list[0]

    def record_sect_skill_response(self, resp):
        """解析宗门传功回复，更新已传功次数"""
        return self.common_record_sect_skill_response(resp, max_daily=SECT_SKILL_MAX_DAILY)

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

    async def run_treasure_touch_loop(self):
        """抚摸法宝循环：到冷却发指令"""
        return await self.run_common_treasure_touch_loop(
            TREASURE_TOUCH_COMMAND,
            sleep_func=scheduler_sleep_seconds,
            pause_label="Treasure touch loop",
        )

    def create_scheduler_task(self, name, coro_factory):
        """启动并登记后台循环，避免 asyncio task 静默退出后无人察觉。"""
        task = asyncio.create_task(coro_factory(), name=name)
        registry = getattr(self, "_scheduler_task_registry", None)
        if not isinstance(registry, dict):
            registry = {}
            self._scheduler_task_registry = registry
        registry[name] = task

        def _on_done(done_task, task_name=name):
            if not getattr(self, "is_running", True) or done_task.cancelled():
                return
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc:
                log.critical(
                    f"Scheduler task [{task_name}] exited with exception: {exc}",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            else:
                log.critical(f"Scheduler task [{task_name}] exited unexpectedly without exception.")

        task.add_done_callback(_on_done)
        return task

    def dead_scheduler_tasks(self):
        dead = []
        registry = getattr(self, "_scheduler_task_registry", None)
        if not isinstance(registry, dict):
            return dead
        for name, task in list(registry.items()):
            if task.done():
                dead.append(name)
        return dead

    def stale_scheduler_due_items(self, overdue_seconds=SCHEDULER_STALE_DUE_SECONDS):
        """找出已到期很久却仍未推进的关键调度项。"""
        if self.state.get("is_paused") or self.main_soul_pause_seconds() > 0:
            return []
        specs = (
            ("next_yuanying_out_time", ".元婴出窍", "主魂"),
            ("next_rift_search_time", ".探寻裂缝", "主魂"),
            ("next_beast_border_patrol_time", ".灵兽巡边", "主魂"),
        )
        stale = []
        now = datetime.now()
        for key, command, identity in specs:
            if self.state_time_command_paused(key, identity) or self.dashboard_command_paused(command, identity):
                continue
            value = str(self.state.get(key) or "").strip()
            if not value or is_future(value):
                continue
            try:
                overdue = int((now - str_to_dt(value)).total_seconds())
            except Exception:
                continue
            if overdue >= int(overdue_seconds):
                stale.append((key, command, value, overdue))
        avatar_specs = (
            (TAIYI_GUIDE_AVATAR, "next_taiyi_guide_time", TAIYI_GUIDE_COMMAND),
        )
        for identity, key, command in avatar_specs:
            if identity not in (getattr(self, "avatars", []) or []):
                continue
            if self.identity_pause_seconds(identity) > 0:
                continue
            if self.state_time_command_paused(key, identity) or self.dashboard_command_paused(command, identity):
                continue
            avatar_state = self.get_avatar_state(identity)
            value = str(avatar_state.get(key) or "").strip()
            if not value or is_future(value):
                continue
            try:
                overdue = int((now - str_to_dt(value)).total_seconds())
            except Exception:
                continue
            if overdue >= int(overdue_seconds):
                stale.append((f"{identity}.{key}", command, value, overdue))
        return stale

    async def stop_for_rift_weakness(self, response, identity="主魂", msg=None):
        """检测到元婴虚弱期：只暂停触发身份，其他身份继续执行。"""
        identity = str(identity or "").strip() or "主魂"
        pause_until = self.mark_identity_rift_rebirth_pending(identity, response, source=".探寻裂缝")
        await send_text_alert(
            self, "万灵宗探寻裂缝告警",
            f"探寻裂缝触发元婴虚弱期，小号身份【{identity}】已暂停；其他身份继续执行。\n\n"
            f"恢复条件：发送 `.重生 1` / `.重生 2` / `.重生 3` 任一成功后自动恢复。\n"
            f"当前状态：{pause_until}\n\n机器人回复：\n{response}",
            log,
        )
        log.critical(
            f"Rift weakness detected. Identity [{identity}] paused until rebirth succeeds; "
            f"other identities continue.\n{response}"
        )

    async def _avatar_yuanying_out_check(self, avatar):
        """分身元婴出窍检查，状态写入分身自己的 state。"""
        return await self.common_avatar_yuanying_out_check(avatar, require_meditation_ready=False)

    async def _avatar_rift_search_check(self, avatar):
        """分身探寻裂缝检查；虚弱结果只暂停触发身份。"""
        return await self.common_avatar_rift_search_check(
            avatar,
            RIFT_SEARCH_CD_SECONDS,
            require_meditation_ready=False,
        )

    async def run_avatar_yuanying_rift_loop(self, avatar, initial_delay=0):
        """Run avatar .元婴出窍 and .探寻裂缝 on their independent cooldowns."""
        return await self.run_common_avatar_yuanying_rift_loop(
            avatar,
            initial_delay=initial_delay,
            sleep_func=scheduler_sleep_seconds,
        )

    async def run_yuanying_out_loop(self):
        """元婴出窍循环：到点自动归窍，再重新出窍"""
        await self.startup_done.wait()
        while self.is_running:
            if await self.sleep_if_main_soul_paused("Yuanying out loop"):
                continue
            wait_time = await self.common_main_yuanying_out_tick(require_yuanying_level=True)
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    async def run_rift_search_loop(self):
        """探寻裂缝循环：定时发送.探寻裂缝"""
        await self.startup_done.wait()
        while self.is_running:
            if await self.sleep_if_main_soul_paused("Rift search loop"):
                continue
            wait_time = await self.common_main_rift_search_tick(
                RIFT_SEARCH_CD_SECONDS,
                require_yuanying_level=True,
            )
            if wait_time < 0:
                break
            await asyncio.sleep(scheduler_sleep_seconds(wait_time))

    # ---- 灵兽：时间解析与状态判断 ----

    def parse_duration_seconds(self, text):
        """从文本解析时长（秒）"""
        if not text: return -1
        clean = text.replace('**', '').replace(' ', '')
        h = re.search(r'(\d+)(?:小时|h)', clean)
        m = re.search(r'(\d+)(?:分钟|分|m)', clean)
        s = re.search(r'(\d+)(?:秒|s)', clean)
        sec = 0; found = False
        if h: sec += int(h.group(1)) * 3600; found = True
        if m: sec += int(m.group(1)) * 60; found = True
        if s: sec += int(s.group(1)); found = True
        return sec if found else -1

    def is_fake_beast_status_response(self, text):
        """检测是否为可通过状态切换清除的忙碌状态。"""
        if not text: return False
        clean = text.replace("**", "")
        return self.is_abyss_busy_response(clean)

    def is_beast_injury_response(self, text):
        """检测是否为真实的灵兽受伤回复"""
        if not text or self.is_fake_beast_status_response(text): return False
        return any(k in text for k in ["重伤", "受伤", "击伤", "伤势", "治疗", "休养"])

    def parse_injury_wait_time(self, text):
        """从受伤回复中解析恢复时间"""
        if not self.is_beast_injury_response(text): return -1
        clean = text.replace('**', '').replace(' ', '')
        candidates = []
        for line in clean.split('\n'):
            if any(k in line for k in ["重伤", "受伤", "击伤", "伤势", "治疗", "恢复", "休养"]):
                sec = self.parse_duration_seconds(line)
                if sec > 0: candidates.append(sec)
        return max(candidates) if candidates else self.parse_duration_seconds(clean)

    # ---- 灵兽：缓存与跟踪 ----

    def sorted_beasts_by_power(self, cache=None):
        """按战力、经验、名称稳定排序灵兽缓存。"""
        beasts = [
            beast for beast in list(cache if cache is not None else self.state.get("beasts_cache", []))
            if self.is_valid_beast_record(beast)
        ]
        beasts.sort(key=lambda x: (x.get('power', 0), x.get('exp', 0), x.get('full_name', '')), reverse=True)
        return beasts

    def is_valid_beast_name(self, name):
        """过滤消息标题/道具名等被误解析成灵兽名的文本。"""
        clean = str(name or "").replace("**", "").strip()
        if not clean:
            return False
        if clean in {"【灵兽归来】", "灵兽归来"}:
            return False
        if clean.startswith("【") and clean.endswith("】") and "灵兽" in clean:
            return False
        if any(k in clean for k in ["伙伴们", "结算如下", "道友 @", "体力恢复"]):
            return False
        return True

    def is_valid_beast_record(self, beast):
        if not isinstance(beast, dict):
            return False
        name = beast.get("full_name", "")
        if not self.is_valid_beast_name(name):
            return False
        species = str(beast.get("species") or "").strip()
        power = beast.get("power", 0)
        stamina = beast.get("stamina", -1)
        try:
            power = int(power or 0)
        except Exception:
            power = 0
        try:
            stamina = int(stamina)
        except Exception:
            stamina = -1
        return bool(species and power > 0 and stamina >= 0)

    def beast_tier_value(self, beast):
        """解析灵兽阶位；返回 1/2/3...，无法识别返回 0。"""
        if isinstance(beast, dict):
            raw = beast.get("tier", 0)
            try:
                if int(raw) > 0:
                    return int(raw)
            except Exception:
                pass
            text = str(beast.get("species") or beast.get("type") or beast.get("full_name") or "")
        else:
            text = str(beast or "")
        match = re.search(r"([一二三四五六七八九十\d]+)\s*阶", text)
        if not match:
            return 0
        value = match.group(1)
        if value.isdigit():
            return int(value)
        chinese = {
            "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        if value == "十":
            return 10
        if value.startswith("十"):
            return 10 + chinese.get(value[1:], 0)
        if value.endswith("十"):
            return chinese.get(value[:-1], 0) * 10
        if "十" in value:
            left, right = value.split("十", 1)
            return chinese.get(left, 0) * 10 + chinese.get(right, 0)
        return chinese.get(value, 0)

    def beast_stamina_value(self, beast):
        try:
            return int(beast.get("stamina", -1))
        except Exception:
            return -1

    def focus_beast_stamina(self, cache=None):
        beast = self.get_cached_beast_by_name(BEAST_FOCUS_NAME) if cache is None else None
        if cache is not None:
            for item in cache:
                if self.beast_name_matches(item.get("full_name", ""), BEAST_FOCUS_NAME):
                    beast = item
                    break
        return self.beast_stamina_value(beast) if beast else -1

    def focus_beast_low_stamina(self, cache=None):
        stamina = self.focus_beast_stamina(cache)
        return 0 <= stamina < BEAST_FOCUS_PROTECT_STAMINA

    def beast_action_candidates(self, action, cache=None, min_stamina=0):
        """Return healthy candidates for an action; protect 六翼 below 50 stamina."""
        protected_focus = self.focus_beast_low_stamina(cache)
        candidates = []
        for beast in self.sorted_beasts_by_power(cache):
            name = beast.get("full_name", "")
            if protected_focus and self.beast_name_matches(name, BEAST_FOCUS_NAME):
                log.info(f"Beast {action}: skipping {BEAST_FOCUS_NAME}, stamina below {BEAST_FOCUS_PROTECT_STAMINA}.")
                continue
            stamina = self.beast_stamina_value(beast)
            if stamina >= 0 and stamina < min_stamina:
                log.info(f"Beast {action}: skipping {name}, stamina {stamina} < {min_stamina}.")
                continue
            status = beast.get("status", "未知")
            if action == "abyss":
                if not self.can_attempt_abyss_status(status):
                    log.info(f"Abyss candidate skipped: {name} status is {status}.")
                    continue
            elif action == "steal":
                if not self.can_attempt_steal_status(status):
                    log.info(f"Steal candidate skipped: {name} status is {status}.")
                    continue
            elif action == "cruise":
                if (
                    status == "未知"
                    or self.is_pastured_status(status)
                    or self.is_injury_status(status)
                    or any(k in status for k in ["探险", "偷菜", "巡游", "巡边"])
                ):
                    log.info(f"Cruise candidate skipped: {name} status is {status}.")
                    continue
            candidates.append(beast)
        candidates.sort(
            key=lambda b: (
                self.beast_stamina_value(b),
                b.get("power", 0),
                b.get("exp", 0),
                b.get("full_name", ""),
            ),
            reverse=True,
        )
        return candidates

    def steal_candidate_beasts(self, cache=None):
        """偷菜候选：首选六翼，不可用时按体力/战力补位。"""
        candidates = self.beast_action_candidates("steal", cache, BEAST_STEAL_MIN_STAMINA)
        preferred = None
        fallback = []
        for beast in candidates:
            if self.beast_name_matches(beast.get("full_name", ""), BEAST_STEAL_PREFERRED_NAME):
                preferred = beast
            else:
                fallback.append(beast)
        ordered = []
        if preferred:
            ordered.append(preferred)
            log.info(f"Steal candidate priority: {BEAST_STEAL_PREFERRED_NAME}.")
        ordered.extend(fallback)
        return ordered

    def select_beast_for_steal(self, cache=None):
        candidates = self.steal_candidate_beasts(cache)
        return candidates[0] if candidates else None

    def select_beast_for_cruise(self, cache=None):
        candidates = self.beast_action_candidates("cruise", cache, BEAST_CRUISE_MIN_STAMINA)
        return candidates[0] if candidates else None

    def border_patrol_candidate_beasts(self, cache=None, exclude_names=None):
        exclude_names = list(exclude_names or [])
        candidates = []
        for beast in list(cache if cache is not None else self.state.get("beasts_cache", [])):
            if not self.is_valid_beast_record(beast):
                continue
            status = beast.get("status", "未知")
            name = beast.get("full_name", "")
            if self.beast_name_matches(name, BEAST_FOCUS_NAME):
                log.info(f"Border patrol candidate skipped: {BEAST_FOCUS_NAME} is reserved for abyss/steal/pasture.")
                continue
            if any(self.beast_name_matches(name, excluded) for excluded in exclude_names):
                log.info(f"Border patrol candidate skipped: {name} already failed this round.")
                continue
            if miniapp_beast_abyss_power_in_range(
                beast.get("power"),
                match_all_when_empty=False,
            ):
                log.info(f"Border patrol candidate skipped: {name} is reserved for abyss power range.")
                continue
            if "休息" not in status:
                log.info(f"Border patrol candidate skipped: {name} status is {status}.")
                continue
            if self.is_injury_status(status) or self.is_pastured_status(status):
                log.info(f"Border patrol candidate skipped: {name} status is {status}.")
                continue
            stamina = self.beast_stamina_value(beast)
            if 0 <= stamina < BEAST_BORDER_PATROL_MIN_STAMINA:
                log.info(
                    f"Border patrol candidate skipped: {name} stamina "
                    f"{stamina} < {BEAST_BORDER_PATROL_MIN_STAMINA}."
                )
                continue
            candidates.append(beast)
        candidates.sort(
            key=lambda b: (
                self.beast_stamina_value(b),
                b.get("power", 0),
                b.get("exp", 0),
                b.get("full_name", ""),
            ),
            reverse=True,
        )
        return candidates

    def select_beast_for_border_patrol(self, cache=None, exclude_names=None):
        candidates = self.border_patrol_candidate_beasts(cache, exclude_names=exclude_names)
        return candidates[0] if candidates else None

    def select_beast_to_recall_for_border_patrol(self, cache=None, exclude_names=None):
        exclude_names = list(exclude_names or [])
        candidates = []
        for beast in list(cache if cache is not None else self.state.get("beasts_cache", [])):
            if not self.is_valid_beast_record(beast):
                continue
            status = beast.get("status", "未知")
            name = beast.get("full_name", "")
            if self.beast_name_matches(name, BEAST_FOCUS_NAME):
                log.info(f"Border patrol recall candidate skipped: {BEAST_FOCUS_NAME} is reserved for abyss/steal/pasture.")
                continue
            if any(self.beast_name_matches(name, excluded) for excluded in exclude_names):
                log.info(f"Border patrol recall candidate skipped: {name} already failed this round.")
                continue
            if miniapp_beast_abyss_power_in_range(
                beast.get("power"),
                match_all_when_empty=False,
            ):
                log.info(f"Border patrol recall candidate skipped: {name} is reserved for abyss power range.")
                continue
            if "休息" in status:
                log.info(f"Border patrol recall candidate skipped: {name} is already resting but unsuitable.")
                continue
            if self.is_injury_status(status) or "巡边" in status:
                log.info(f"Border patrol recall candidate skipped: {name} status is {status}.")
                continue
            stamina = self.beast_stamina_value(beast)
            # 放养期间缓存仍是出发前体力，可能已经恢复；召回后用巡边回执确认真实体力。
            if 0 <= stamina < BEAST_BORDER_PATROL_MIN_STAMINA and not self.is_pastured_status(status):
                log.info(
                    f"Border patrol recall candidate skipped: {name} stamina "
                    f"{stamina} < {BEAST_BORDER_PATROL_MIN_STAMINA}."
                )
                continue
            candidates.append(beast)
        candidates.sort(
            key=lambda b: (
                self.beast_stamina_value(b),
                b.get("power", 0),
                b.get("exp", 0),
                b.get("full_name", ""),
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def get_best_beast(self):
        """从缓存中获取战力最高的灵兽"""
        cache = self.sorted_beasts_by_power()
        return cache[0] if cache else None

    def update_best_beast_tracking(self, best=None):
        """更新最佳灵兽跟踪信息"""
        best = best or self.get_best_beast()
        if not best: return
        old_name = self.state.get("best_beast_name", "")
        self.state["best_beast_name"] = best.get("full_name", "")
        self.state["best_beast_power"] = best.get("power", 0)
        self.state["best_beast_status"] = best.get("status", "未知")
        self.state["best_beast_stamina"] = self.beast_stamina_value(best)
        self.record_best_beast_status_timing(best.get("status", ""), best.get("status_cd", -1))
        if old_name and old_name != self.state["best_beast_name"]:
            log.info(f"Best beast changed: {old_name} -> {self.state['best_beast_name']}.")

    def adjust_pasture_pending_for_status_change(self, name, old_status, new_status):
        """灵兽状态变化时调整放养待处理计数"""
        old_status = old_status or ""; new_status = new_status or ""
        if "放养" not in old_status or "放养" in new_status: return False
        pending = self.pasture_pending_count(); returned = self.pasture_returned_count()
        if pending <= 0 or returned >= pending: return False
        adjusted_pending = max(returned, pending - 1)
        self.state["pasture_pending_count"] = adjusted_pending
        if returned >= adjusted_pending:
            extra_changed = self.mark_all_pastured_beasts_returned()
            if extra_changed: log.info(f"Pasture adjusted complete: normalized {extra_changed} cached beasts.")
            self.clear_pasture_pending()
            self.state["next_pasture_time"] = add_seconds_str(now_str(), PASTURE_RETURN_DELAY_SECONDS)
        return True

    def set_best_beast_status(self, name, status, status_cd=-1):
        """设置最佳灵兽的状态并保存"""
        if not name or not status: return
        old_status = ""
        if self.beast_name_matches(self.state.get("best_beast_name", ""), name):
            old_status = self.state.get("best_beast_status", "")
        for beast in self.state.get("beasts_cache", []):
            if self.beast_name_matches(beast.get("full_name", ""), name):
                old_status = beast.get("status", "") or old_status
                break
        self.adjust_pasture_pending_for_status_change(name, old_status, status)
        if self.beast_name_matches(self.state.get("best_beast_name", ""), name):
            self.state["best_beast_status"] = status
            self.record_best_beast_status_timing(status, status_cd)
        for beast in self.state.get("beasts_cache", []):
            if self.beast_name_matches(beast.get("full_name", ""), name):
                beast["full_name"] = name
                beast["status"] = status
                if status_cd is not None:
                    beast["status_cd"] = status_cd
                break
        self.save_state()

    def set_cached_beast_stamina(self, name, stamina):
        """更新缓存中某只灵兽的体力。"""
        if not name:
            return
        try:
            stamina = int(stamina)
        except Exception:
            return
        for beast in self.state.get("beasts_cache", []):
            if beast.get("full_name") == name:
                beast["stamina"] = stamina
                break
        if self.state.get("best_beast_name") == name:
            self.state["best_beast_stamina"] = stamina
        self.save_state()

    def is_injury_status(self, status):
        return any(k in (status or "") for k in ["受伤", "重伤", "治疗"])

    def is_pastured_status(self, status):
        return "放养" in (status or "")

    def is_beast_pastured_response(self, text, beast_name=BEAST_FOCUS_NAME):
        """检测灵兽因放养中被阻塞的明确回复。"""
        clean = str(text or "").replace("**", "")
        if "放养" not in clean:
            return False
        if beast_name and beast_name not in clean and BEAST_FOCUS_NAME not in clean and "灵兽" not in clean:
            return False
        return any(k in clean for k in ["放养中", "正在放养", "无需召回", "无法立刻出战", "暂时无法互动"])

    def set_state_time_not_before(self, key, target_time):
        """把 state 中的时间字段设置为不早于 target_time。"""
        if not key or not target_time:
            return
        target_dt = str_to_dt(target_time) if isinstance(target_time, str) else target_time
        current = self.state.get(key, "")
        if current:
            target_dt = max(target_dt, str_to_dt(current))
        self.state[key] = dt_to_str(target_dt)

    def pasture_block_until(self, fallback_seconds=1800):
        """估算放养阻塞结束时间，优先使用 .一键放养 已记录的冷却。"""
        candidates = []
        next_pasture = self.state.get("next_pasture_time", "")
        if next_pasture and is_future(next_pasture):
            candidates.append(str_to_dt(next_pasture))
        last_pasture = self.state.get("last_pasture_time", "")
        if last_pasture:
            inferred = str_to_dt(last_pasture) + timedelta(seconds=PASTURE_CD_SECONDS)
            if inferred > datetime.now():
                candidates.append(inferred)
        pending_since = self.state.get("pasture_pending_since", "")
        if pending_since:
            inferred = str_to_dt(pending_since) + timedelta(seconds=PASTURE_CD_SECONDS)
            if inferred > datetime.now():
                candidates.append(inferred)
        if not candidates:
            candidates.append(datetime.now() + timedelta(seconds=fallback_seconds))
        return dt_to_str(max(candidates))

    def defer_beast_actions_while_pastured(self, beast_name=BEAST_FOCUS_NAME, reason=""):
        """灵兽放养中时，仅暂停依赖该放养状态的低优先级动作。"""
        beast_name = beast_name or BEAST_FOCUS_NAME
        if self.beast_name_matches(beast_name, BEAST_FOCUS_NAME):
            return self.defer_focus_beast_personal_actions_while_pastured(reason)

        target_time = self.pasture_block_until()
        self.set_best_beast_status(beast_name, "放养中")
        for key in (
            "next_beast_status_check_time",
            "next_pasture_time",
        ):
            self.set_state_time_not_before(key, target_time)
        log.info(
            f"Beast actions deferred until {target_time}: {beast_name} is pastured"
            f"{f' ({reason})' if reason else ''}."
        )
        self.save_state()
        return target_time

    def defer_focus_beast_personal_actions_while_pastured(self, reason=""):
        """六翼放养中只暂停六翼自己的动作，其他灵兽仍可补位执行。"""
        target_time = self.pasture_block_until()
        self.set_best_beast_status(BEAST_FOCUS_NAME, "放养中")
        for key in ("next_pasture_time",):
            self.set_state_time_not_before(key, target_time)
        log.info(
            f"Focus beast personal actions deferred until {target_time}: {BEAST_FOCUS_NAME} is pastured"
            f"{f' ({reason})' if reason else ''}."
        )
        self.save_state()
        return target_time

    def record_best_beast_status_timing(self, status, status_cd=-1):
        """记录灵兽受伤/恢复的时间点"""
        status = status or ""
        if self.is_injury_status(status):
            if not self.state.get("best_beast_injured_time"):
                self.state["best_beast_injured_time"] = now_str()
            check_time = self.state.get("next_beast_status_check_time", "")
            if status_cd and status_cd > 0:
                self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), status_cd)
                check_time = self.state["next_beast_status_check_time"]
            elif not check_time:
                self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), BEAST_INJURY_DEFAULT_RETRY_SECONDS)
                check_time = self.state["next_beast_status_check_time"]
            elif not is_future(check_time):
                self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), BEAST_INJURY_DEFAULT_RETRY_SECONDS)
                check_time = self.state["next_beast_status_check_time"]
                log.info("Beast injury status still active; refreshed default recovery window.")
        elif status and "未知" not in status:
            self.state["best_beast_injured_time"] = ""
            self.state["next_beast_status_check_time"] = ""
            self.state["best_beast_injury_source"] = ""

    def is_injury_recovery_pending(self, status):
        return self.is_injury_status(status) and bool(self.state.get("next_beast_status_check_time", "") and is_future(self.state["next_beast_status_check_time"]))

    def is_stale_injury_status(self, status):
        return self.is_injury_status(status) and not self.is_injury_recovery_pending(status)

    def should_rest_before_abyss(self, status):
        """判断探渊前是否需要先休息灵兽"""
        status = status or ""
        return self.is_pastured_status(status) or "出战" in status or self.is_stale_injury_status(status)

    def can_attempt_abyss_status(self, status):
        """判断灵兽状态是否允许探渊"""
        status = status or ""
        if self.is_injury_status(status): return False
        return not any(k in status for k in ["受伤", "重伤", "治疗", "探险", "偷菜", "巡游", "巡边"])

    def can_attempt_steal_status(self, status):
        """判断灵兽状态是否允许偷菜"""
        status = status or ""
        if any(k in status for k in ["探险", "偷菜", "巡游", "巡边"]): return False
        if self.is_injury_status(status): return False
        return True

    # ---- 灵兽：互动/巡游 ----

    def get_cached_beast_by_name(self, target_name=BEAST_FOCUS_NAME):
        """从缓存中查找指定灵兽，支持忽略括号后缀匹配。"""
        for beast in self.state.get("beasts_cache", []):
            if self.beast_name_matches(beast.get("full_name", ""), target_name):
                return beast
        return None

    def no_such_beast_name_from_response(self, text):
        clean = str(text or "").replace("**", "")
        match = re.search(r"没有名为[“\"【]?([^”\"】]+)[”\"】]?的灵兽", clean)
        return match.group(1).strip() if match else ""

    def remove_cached_beast(self, beast_name, reason=""):
        beast_name = str(beast_name or "").strip()
        if not beast_name:
            return False
        old_cache = list(self.state.get("beasts_cache", []))
        new_cache = [
            beast for beast in old_cache
            if not self.beast_name_matches(beast.get("full_name", ""), beast_name)
        ]
        changed = len(new_cache) != len(old_cache)
        if changed:
            self.state["beasts_cache"] = new_cache
        if self.beast_name_matches(self.state.get("best_beast_name", ""), beast_name):
            changed = True
            self.state["best_beast_name"] = ""
            self.state["best_beast_power"] = 0
            self.state["best_beast_status"] = ""
            self.state["best_beast_stamina"] = -1
            self.state["best_beast_injury_source"] = ""
            self.state["best_beast_injured_time"] = ""
        if changed:
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 60)
            self.save_state()
            log.info(f"Removed stale beast cache entry [{beast_name}] after no-such-beast response ({reason}).")
        return changed

    def handle_no_such_beast_response(self, beast_name, response_text, context=""):
        if not self.is_no_such_beast_response(response_text):
            return False
        missing = self.no_such_beast_name_from_response(response_text) or beast_name
        changed = self.remove_cached_beast(missing, context)
        if not changed:
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 60)
            self.save_state()
            log.info(f"Scheduled beast roster refresh after no-such-beast response for [{missing}] ({context}).")
        return True

    def schedule_beast_action_retry(self, next_key, retry_seconds=BEAST_ACTION_RETRY_SECONDS):
        self.state[next_key] = add_seconds_str(now_str(), retry_seconds)
        self.save_state()

    def beast_interaction_command_for_status(self, status):
        """六翼受伤时用安抚，否则用常规抚摸。"""
        if self.is_injury_status(status):
            return BEAST_SOOTHE_COMMAND
        return BEAST_INTERACTION_COMMAND

    def record_beast_interaction_response(self, resp, command=BEAST_INTERACTION_COMMAND):
        """解析 .灵兽互动 六翼 / .灵兽互动 六翼 安抚 回复并记录 90 分钟冷却。"""
        last_key = "last_beast_interaction_time"
        next_key = "next_beast_interaction_time"
        if not resp:
            self.schedule_beast_action_retry(next_key)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚需", "还需", "请在", "休息"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            return True
        if command == BEAST_INTERACTION_COMMAND and self.is_beast_injury_response(resp):
            injury_cd = self.record_beast_injury_from_response(BEAST_FOCUS_NAME, resp, source="interaction")
            retry_seconds = 60 if injury_cd < 0 else min(max(60, injury_cd), BEAST_ACTION_RETRY_SECONDS)
            self.schedule_beast_action_retry(next_key, retry_seconds)
            log.info(
                f"Beast interaction touch rejected because {BEAST_FOCUS_NAME} is injured; "
                f"will retry with soothe in {retry_seconds}s."
            )
            return True
        if self.is_beast_pastured_response(resp, BEAST_FOCUS_NAME):
            self.defer_beast_actions_while_pastured(BEAST_FOCUS_NAME, "interaction response")
            return True
        if self.handle_no_such_beast_response(BEAST_FOCUS_NAME, resp, "interaction response"):
            self.schedule_beast_action_retry(next_key, 1800)
            return True
        if self.record_beast_current_status_response(resp, BEAST_FOCUS_NAME, "interaction response", next_key):
            return True
        if any(k in resp for k in ["没有这只灵兽", "未找到该灵兽", "不存在该灵兽", "无法", "尚未"]):
            self.schedule_beast_action_retry(next_key)
            return True
        if any(k in resp for k in [
            "灵兽", BEAST_FOCUS_NAME, "抚摸", "安抚", "互动", "亲密",
            "心情", "开心", "愉悦", "情绪", "忠诚", "羁绊",
            "经验", "获得", "增加", "好转", "平静", "安静",
        ]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, BEAST_INTERACTION_CD_SECONDS)
            if not self.is_beast_injury_response(resp):
                self.set_best_beast_status(BEAST_FOCUS_NAME, "休息中")
            return True
        self.schedule_beast_action_retry(next_key)
        notify_unrecognized_response(self, command, resp, log, "灵兽互动")
        return False

    def record_beast_cruise_response(self, resp, beast_name=BEAST_FOCUS_NAME):
        """解析 .灵兽巡游 <灵兽> 回复并记录 120 分钟冷却。"""
        beast_name = beast_name or BEAST_FOCUS_NAME
        command = f".灵兽巡游 {beast_name}"
        last_key = "last_beast_cruise_time"
        next_key = "next_beast_cruise_time"
        if not resp:
            self.schedule_beast_action_retry(next_key, 1800)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚需", "还需", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            return True
        if self.is_beast_stamina_insufficient_response(resp):
            required = self.parse_required_beast_stamina(resp, default=BEAST_CRUISE_MIN_STAMINA)
            self.set_cached_beast_stamina(beast_name, max(0, required - 1))
            retry = max(BEAST_ACTION_RETRY_SECONDS, 30 * 60)
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), retry)
            self.schedule_beast_action_retry(next_key, retry)
            log.info(f"Beast cruise: {beast_name} stamina below {required}; retry at {self.state[next_key]}.")
            return True
        if self.is_beast_cruise_blocked_by_deployed(resp, beast_name):
            self.set_best_beast_status(beast_name, "出战中")
            self.schedule_beast_action_retry(next_key, 1800)
            return True
        if self.is_beast_pastured_response(resp, beast_name):
            self.defer_beast_actions_while_pastured(beast_name, "cruise response")
            return True
        if self.handle_no_such_beast_response(beast_name, resp, "cruise response"):
            self.schedule_beast_action_retry(next_key, 1800)
            return True
        injury_cd = self.record_beast_injury_from_response(beast_name, resp, source="cruise")
        if injury_cd >= 0:
            self.schedule_beast_action_retry(next_key, max(1800, injury_cd))
            log.info(f"Beast cruise: {beast_name} is injured; status recorded, trying another candidate later.")
            return True
        if self.record_beast_current_status_response(resp, beast_name, "cruise response", next_key):
            return True
        if any(k in resp for k in ["需要休息", "休息状态", "无法巡游", "受伤", "重伤", "治疗"]) or (
            "正在" in resp and "正在巡游" not in resp
        ):
            retry = cd if cd > 0 else 1800
            self.schedule_beast_action_retry(next_key, retry)
            return True
        if any(k in resp for k in ["巡游", "出发", "游历", "带回", "获得", "收获", "成功"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, BEAST_CRUISE_CD_SECONDS)
            return True
        self.schedule_beast_action_retry(next_key, 1800)
        notify_unrecognized_response(self, command, resp, log, "灵兽巡游")
        return False

    def is_beast_cruise_blocked_by_deployed(self, text, beast_name=BEAST_FOCUS_NAME):
        """检测巡游被出战状态阻塞的明确回复。"""
        text = text or ""
        return (not beast_name or beast_name in text or "灵兽" in text) and "无法巡游" in text and "出战" in text

    async def run_focus_beast_cruise(self, beast_name=BEAST_FOCUS_NAME):
        """发送灵兽巡游；若提示出战中无法巡游，先召回休息再重试一次。"""
        beast_name = beast_name or BEAST_FOCUS_NAME
        command = f".灵兽巡游 {beast_name}"
        resp = await self.send_and_wait_feedback(command, timeout=60, max_retries=1)
        if not self.is_beast_cruise_blocked_by_deployed(resp, beast_name):
            self.record_beast_cruise_response(resp, beast_name)
            return

        log.info(f"Beast cruise: {beast_name} is deployed; resting before retry.")
        self.set_best_beast_status(beast_name, "出战中")
        rest_status, rest_resp = await self.rest_beast_for_abyss(beast_name)
        if rest_status and "休息" in rest_status:
            await asyncio.sleep(3)
            retry_resp = await self.send_and_wait_feedback(command, timeout=60, max_retries=1)
            self.record_beast_cruise_response(retry_resp, beast_name)
            return

        injury_cd = self.record_beast_injury_from_response(beast_name, rest_resp, source="cruise")
        retry = injury_cd if injury_cd > 0 else 1800
        self.schedule_beast_action_retry("next_beast_cruise_time", retry)
        if rest_resp and injury_cd < 0 and not self.is_fake_beast_status_response(rest_resp):
            notify_unrecognized_response(self, f"万兽谷灵兽休息 {beast_name}", rest_resp, log, "巡游失败后休息")

    async def prepare_focus_beast_for_cruise(self, beast=None):
        """确保目标灵兽为休息状态；仅出战中会主动召回，其他忙碌/受伤状态延后。"""
        beast = beast or self.select_beast_for_cruise(self.state.get("beasts_cache", []))
        if not beast:
            log.info("Beast cruise: no suitable beast candidate; retry later.")
            self.schedule_beast_action_retry("next_beast_cruise_time", 1800)
            return False
        beast_name = beast.get("full_name") or BEAST_FOCUS_NAME
        status = beast.get("status", "未知")
        if status == "未知":
            log.info(f"Beast cruise: {beast_name} status unknown; retry after next beast refresh.")
            self.schedule_beast_action_retry("next_beast_cruise_time", 1800)
            return False
        stamina = self.beast_stamina_value(beast)
        if stamina >= 0 and stamina < BEAST_CRUISE_MIN_STAMINA:
            retry = max(BEAST_ACTION_RETRY_SECONDS, 30 * 60)
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), retry)
            log.info(
                f"Beast cruise deferred: {beast_name} stamina {stamina} < "
                f"{BEAST_CRUISE_MIN_STAMINA}, retry in {retry}s."
            )
            self.schedule_beast_action_retry("next_beast_cruise_time", retry)
            return False
        if "休息" in status:
            return True
        if self.is_pastured_status(status):
            self.defer_beast_actions_while_pastured(beast_name, "cruise precheck")
            return False
        if "出战" in status:
            rest_status, rest_resp = await self.rest_beast_for_abyss(beast_name)
            if rest_status and "休息" in rest_status:
                return True
            injury_cd = self.record_beast_injury_from_response(beast_name, rest_resp, source="cruise")
            retry = injury_cd if injury_cd > 0 else 1800
            self.schedule_beast_action_retry("next_beast_cruise_time", retry)
            if rest_resp and injury_cd < 0 and not self.is_fake_beast_status_response(rest_resp):
                notify_unrecognized_response(self, f"万兽谷灵兽休息 {beast_name}", rest_resp, log, "巡游前休息")
            return False
        retry_time = self.state.get("next_beast_status_check_time", "")
        retry_seconds = int(seconds_until(retry_time)) if retry_time and is_future(retry_time) else 1800
        log.info(f"Beast cruise deferred: {beast_name} status is {status}, retry in {retry_seconds}s.")
        self.schedule_beast_action_retry("next_beast_cruise_time", max(1800, retry_seconds))
        return False

    # ---- 灵兽：巡边 ----

    def repair_overdue_beast_border_patrol_schedule(self):
        """巡边已到归来时间时，清掉旧唤醒时间并立刻推动归来检查。"""
        name = str(self.state.get("beast_border_patrol_name") or "").strip()
        last_patrol = self.state.get("last_beast_border_patrol_time", "")
        next_patrol = self.state.get("next_beast_border_patrol_time", "")
        if not name or not last_patrol:
            return False
        due_at = str_to_dt(last_patrol) + timedelta(seconds=BEAST_BORDER_PATROL_CD_SECONDS)
        if due_at > datetime.now():
            return False
        self.state["next_beast_border_patrol_time"] = ""
        self.save_state()
        wakeup = getattr(self, "beast_wakeup", None)
        if wakeup:
            wakeup.set()
        delayed_note = (
            f" but next patrol had been delayed until {next_patrol}"
            if next_patrol and is_future(next_patrol)
            else f"; stale next patrol was {next_patrol or '<empty>'}"
        )
        log.warning(
            f"Beast border patrol schedule repaired: {name} was due at {dt_to_str(due_at)}"
            f"{delayed_note}; clearing it for immediate return."
        )
        return True

    def wake_overdue_beast_border_patrol(self, reason=""):
        """巡边冷却已过但被重试时间压住时，清掉重试并让主循环立刻尝试。"""
        if str(self.state.get("beast_border_patrol_name") or "").strip():
            return False
        last_patrol = self.state.get("last_beast_border_patrol_time", "")
        if last_patrol and is_future(add_seconds_str(last_patrol, BEAST_BORDER_PATROL_CD_SECONDS)):
            return False
        next_patrol = self.state.get("next_beast_border_patrol_time", "")
        if not next_patrol or not is_future(next_patrol):
            return False
        log.info(
            f"Beast border patrol wakeup: cooldown elapsed; clearing retry {next_patrol}"
            f"{f' after {reason}' if reason else ''}."
        )
        self.state["next_beast_border_patrol_time"] = ""
        return True

    def normalize_beast_border_patrol_mode(self, mode=""):
        mode = str(mode or "").strip()
        return mode if mode in BEAST_BORDER_PATROL_MODES else BEAST_BORDER_PATROL_DEFAULT_MODE

    def configured_beast_border_patrol_mode(self):
        reader = getattr(self, "dashboard_command_option", None)
        mode = reader(
            ".灵兽巡边 <灵兽> 袭营",
            "patrol_mode",
            BEAST_BORDER_PATROL_DEFAULT_MODE,
            "主魂",
        ) if callable(reader) else BEAST_BORDER_PATROL_DEFAULT_MODE
        return self.normalize_beast_border_patrol_mode(mode)

    def parse_beast_border_patrol_command(self, command):
        parts = str(command or "").strip().split()
        if len(parts) < 2:
            return "", BEAST_BORDER_PATROL_DEFAULT_MODE
        beast_name = parts[1].strip()
        mode = parts[2].strip() if len(parts) >= 3 else BEAST_BORDER_PATROL_DEFAULT_MODE
        return beast_name, self.normalize_beast_border_patrol_mode(mode)

    def border_patrol_beast_name_from_text(self, text):
        clean = str(text or "").replace("**", "")
        for pattern in (
            r"灵兽[:：]\s*([^\n\r]+)",
            r"灵兽【([^】]+)】",
            r"【([^】]+)】(?:正在)?(?:边境|巡边|巡行)",
        ):
            m = re.search(pattern, clean)
            if m:
                name = re.sub(r"\s*\([^)]*\)", "", m.group(1)).strip()
                if self.is_valid_beast_name(name):
                    return name
        return ""

    def is_existing_border_patrol_response(self, text):
        clean = str(text or "").replace("**", "")
        return "已有灵兽正在边境巡行" in clean or ("已有灵兽" in clean and any(k in clean for k in ["巡边", "巡行", "边境"]))

    def is_border_patrol_status_clear_response(self, text):
        clean = str(text or "").replace("**", "")
        return any(k in clean for k in ["暂无灵兽巡边", "没有灵兽巡边", "无灵兽巡边", "未派遣灵兽巡边", "当前没有灵兽"])

    def record_beast_border_patrol_status_response(self, resp):
        if not resp:
            self.schedule_beast_action_retry("next_beast_border_patrol_time", 600)
            return False
        if self.is_border_patrol_status_clear_response(resp):
            self.state["beast_border_patrol_name"] = ""
            self.state["next_beast_border_patrol_time"] = ""
            self.save_state()
            return True
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["巡边", "巡行", "边境", "剩余", "还需", "尚需", "预计"]):
            name = self.border_patrol_beast_name_from_text(resp)
            if name:
                self.state["beast_border_patrol_name"] = name
                self.set_best_beast_status(name, "巡边中")
            self.state["next_beast_border_patrol_time"] = add_seconds_str(now_str(), cd)
            self.save_state()
            return True
        if any(k in resp for k in ["巡边", "巡行", "边境"]):
            name = self.border_patrol_beast_name_from_text(resp)
            if name:
                self.state["beast_border_patrol_name"] = name
                self.set_best_beast_status(name, "巡边中")
            self.schedule_beast_action_retry("next_beast_border_patrol_time", BEAST_ACTION_RETRY_SECONDS)
            return True
        self.schedule_beast_action_retry("next_beast_border_patrol_time", BEAST_BORDER_PATROL_CD_SECONDS)
        notify_unrecognized_response(self, ".巡边状态", resp, log, "灵兽巡边状态")
        return False

    def record_border_patrol_candidate_unavailable_response(self, resp, beast_name=""):
        """记录单只灵兽暂时不能巡边；返回是否也要排除召回兜底。"""
        clean = str(resp or "").replace("**", "")
        if not clean or self.is_existing_border_patrol_response(clean) or self.is_beast_stamina_insufficient_response(clean):
            return None

        cd = self.parse_wait_time(clean)
        if cd > 0 and any(k in clean for k in ["冷却", "后再", "尚需", "还需", "请在"]):
            return None

        injury_cd = self.record_beast_injury_from_response(beast_name, clean, source="border patrol")
        if injury_cd >= 0:
            log.info(f"Beast border patrol: {beast_name} unavailable due to injury; trying fallback candidate.")
            return True

        resp_name, resp_status = self.parse_beast_current_status_response(clean)
        target_name = resp_name or beast_name
        if target_name and resp_status and "巡边" not in resp_status:
            self.set_best_beast_status(target_name, resp_status)
            log.info(
                f"Beast border patrol: {target_name} is {resp_status}, "
                "trying another resting candidate before recall fallback."
            )
            return bool(self.is_injury_status(resp_status))

        named_candidate = bool(
            resp_name
            or (beast_name and beast_name in clean)
            or "灵兽【" in clean
        )
        if named_candidate and any(k in clean for k in ["需要休息", "休息状态", "无法巡边", "受伤", "重伤", "治疗"]):
            log.info(
                f"Beast border patrol: {beast_name} cannot patrol from response "
                f"{clean[:80]}; trying fallback candidate."
            )
            self.save_state()
            return True
        return None

    def record_beast_border_patrol_response(self, resp, beast_name="", mode=BEAST_BORDER_PATROL_DEFAULT_MODE):
        beast_name = beast_name or self.border_patrol_beast_name_from_text(resp)
        mode = self.normalize_beast_border_patrol_mode(mode)
        if not resp:
            self.schedule_beast_action_retry("next_beast_border_patrol_time", 600)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚需", "还需", "请在", "巡边", "巡行", "边境"]):
            now = now_str()
            self.state["last_beast_border_patrol_time"] = now
            self.state["next_beast_border_patrol_time"] = add_seconds_str(now, cd)
            if beast_name:
                self.state["beast_border_patrol_name"] = beast_name
                self.set_best_beast_status(beast_name, "巡边中")
            self.state["beast_border_patrol_mode"] = mode
            self.save_state()
            return True
        if self.is_existing_border_patrol_response(resp):
            self.schedule_beast_action_retry("next_beast_border_patrol_time", BEAST_ACTION_RETRY_SECONDS)
            return True
        if self.is_beast_stamina_insufficient_response(resp):
            if beast_name:
                self.record_beast_stamina_shortage(beast_name, resp, "border patrol")
            self.schedule_beast_action_retry("next_beast_border_patrol_time", 1800)
            return True
        if self.handle_no_such_beast_response(beast_name, resp, "border patrol response"):
            self.schedule_beast_action_retry("next_beast_border_patrol_time", 1800)
            return True
        injury_cd = self.record_beast_injury_from_response(beast_name, resp, source="border patrol")
        if injury_cd >= 0:
            self.schedule_beast_action_retry("next_beast_border_patrol_time", max(1800, injury_cd))
            return True
        if self.record_beast_current_status_response(
            resp,
            beast_name,
            "border patrol response",
            "next_beast_border_patrol_time",
        ):
            return True
        if any(k in resp for k in ["需要休息", "休息状态", "无法巡边", "受伤", "重伤", "治疗"]) or (
            "正在" in resp and "边境巡行" not in resp and "巡边" not in resp
        ):
            self.schedule_beast_action_retry("next_beast_border_patrol_time", cd if cd > 0 else 1800)
            return True
        if any(k in resp for k in ["灵兽巡边", "边境巡行", "巡边", "边境", "斥候", "护粮", "袭营", "出发", "领命", "成功"]):
            now = now_str()
            self.state["last_beast_border_patrol_time"] = now
            self.state["next_beast_border_patrol_time"] = add_seconds_str(now, BEAST_BORDER_PATROL_CD_SECONDS)
            self.state["beast_border_patrol_name"] = beast_name
            self.state["beast_border_patrol_mode"] = mode
            if beast_name:
                self.set_best_beast_status(beast_name, "巡边中")
            self.save_state()
            return True
        self.schedule_beast_action_retry("next_beast_border_patrol_time", BEAST_BORDER_PATROL_CD_SECONDS)
        command = f".灵兽巡边 {beast_name or '<灵兽名>'} {mode}"
        notify_unrecognized_response(self, command, resp, log, "灵兽巡边")
        return False

    def record_beast_border_patrol_return_response(self, resp):
        if not resp:
            return False
        cd = self.parse_wait_time(resp)
        name = self.border_patrol_beast_name_from_text(resp) or self.state.get("beast_border_patrol_name", "")
        if cd > 0 and any(k in resp for k in ["还需", "尚需", "剩余", "冷却", "巡边", "巡行"]):
            if name:
                self.state["beast_border_patrol_name"] = name
                self.set_best_beast_status(name, "巡边中")
            self.state["next_beast_border_patrol_time"] = add_seconds_str(now_str(), cd)
            self.save_state()
            return True
        if any(k in resp for k in ["巡边归来", "归来", "召回", "返回", "带回", "收获", "获得", "已结束"]):
            self.state["next_beast_border_patrol_time"] = ""
            self.state["beast_border_patrol_name"] = ""
            self.clear_border_patrol_cache_statuses()
            if name:
                self.set_best_beast_status(name, "休息中")
            self.save_state()
            return True
        if self.is_border_patrol_status_clear_response(resp):
            self.state["beast_border_patrol_name"] = ""
            self.clear_border_patrol_cache_statuses()
            self.save_state()
            return True
        return False

    async def finish_beast_border_patrol_if_active(self):
        name = str(self.state.get("beast_border_patrol_name") or "").strip()
        if not name:
            return False
        log.info(f"Beast border patrol: {name} is active; sending .巡边归来 before starting another patrol.")
        resp = await self.send_and_wait_feedback(".巡边归来", timeout=60, max_retries=1)
        if self.record_beast_border_patrol_return_response(resp):
            return True
        status_resp = await self.send_and_wait_feedback(".巡边状态", timeout=45, max_retries=1)
        if self.record_beast_border_patrol_status_response(status_resp):
            return True
        self.schedule_beast_action_retry("next_beast_border_patrol_time", BEAST_BORDER_PATROL_CD_SECONDS)
        notify_unrecognized_response(self, ".巡边归来", resp, log, "灵兽巡边归来")
        return False

    async def run_beast_border_patrol(self, mode=BEAST_BORDER_PATROL_DEFAULT_MODE):
        mode = self.normalize_beast_border_patrol_mode(mode)
        if str(self.state.get("beast_border_patrol_name") or "").strip():
            if not await self.finish_beast_border_patrol_if_active():
                return False
            if str(self.state.get("beast_border_patrol_name") or "").strip():
                return True
        patrol_failed_names = []
        recall_failed_names = []
        recall_retry_seconds = []
        last_response = ""

        def remember_failed(target_list, name):
            if name and not any(self.beast_name_matches(name, failed) for failed in target_list):
                target_list.append(name)

        def remember_recall_retry(seconds):
            try:
                seconds = int(seconds or 0)
            except Exception:
                seconds = 0
            if seconds > 0:
                recall_retry_seconds.append(seconds)

        while True:
            beast = self.select_beast_for_border_patrol(
                self.state.get("beasts_cache", []),
                exclude_names=patrol_failed_names,
            )
            if not beast:
                recall_beast = self.select_beast_to_recall_for_border_patrol(
                    self.state.get("beasts_cache", []),
                    exclude_names=recall_failed_names,
                )
                if not recall_beast:
                    retry = min(recall_retry_seconds) + 5 if recall_retry_seconds else 1800
                    retry = max(60, retry)
                    log.info(
                        "Beast border patrol skipped: no resting or recallable beast candidate "
                        f"after stamina fallbacks; retry in {retry}s. Last response: {last_response[:80]}"
                    )
                    self.schedule_beast_action_retry("next_beast_border_patrol_time", retry)
                    return False
                recall_name = recall_beast.get("full_name", "")
                log.info(f"Beast border patrol: no resting candidate; recalling highest-stamina beast {recall_name}.")
                rest_status, rest_resp = await self.rest_beast_for_abyss(recall_name)
                last_response = rest_resp or last_response
                if rest_status and "休息" in rest_status:
                    await asyncio.sleep(3)
                    beast = self.get_cached_beast_by_name(recall_name) or recall_beast
                    beast["status"] = "休息中"
                else:
                    if self.is_existing_border_patrol_response(rest_resp):
                        status_resp = await self.send_and_wait_feedback(".巡边状态", timeout=45, max_retries=1)
                        return self.record_beast_border_patrol_status_response(status_resp)
                    if self.handle_no_such_beast_response(recall_name, rest_resp, "border patrol recall"):
                        remember_failed(recall_failed_names, recall_name)
                        await asyncio.sleep(3)
                        continue
                    injury_cd = self.record_beast_injury_from_response(recall_name, rest_resp, source="border patrol recall")
                    if injury_cd >= 0:
                        remember_failed(recall_failed_names, recall_name)
                        remember_recall_retry(injury_cd)
                        log.info(f"Beast border patrol: {recall_name} recall blocked by injury; trying fallback candidate.")
                        await asyncio.sleep(3)
                        continue
                    if self.is_beast_pastured_response(rest_resp, recall_name) or self.is_pastured_status(rest_status):
                        wait = self.parse_wait_time(rest_resp)
                        remember_recall_retry(wait)
                        self.set_best_beast_status(recall_name, "放养中")
                        remember_failed(recall_failed_names, recall_name)
                        log.info(f"Beast border patrol: {recall_name} is still pastured after recall; trying fallback candidate.")
                        await asyncio.sleep(3)
                        continue
                    remember_failed(recall_failed_names, recall_name)
                    if rest_resp and not self.is_fake_beast_status_response(rest_resp):
                        notify_unrecognized_response(self, f"万兽谷灵兽休息 {recall_name}", rest_resp, log, "巡边前召回")
                    await asyncio.sleep(3)
                    continue
            beast_name = beast.get("full_name", "")
            command = f".灵兽巡边 {beast_name} {mode}"
            resp = await self.send_and_wait_feedback(command, timeout=60, max_retries=1)
            last_response = resp or last_response
            if self.is_existing_border_patrol_response(resp):
                status_resp = await self.send_and_wait_feedback(".巡边状态", timeout=45, max_retries=1)
                return self.record_beast_border_patrol_status_response(status_resp)
            if self.is_beast_stamina_insufficient_response(resp):
                self.record_beast_stamina_shortage(beast_name, resp, "border patrol")
                remember_failed(patrol_failed_names, beast_name)
                remember_failed(recall_failed_names, beast_name)
                await asyncio.sleep(3)
                continue
            if self.handle_no_such_beast_response(beast_name, resp, "border patrol run"):
                remember_failed(patrol_failed_names, beast_name)
                remember_failed(recall_failed_names, beast_name)
                await asyncio.sleep(3)
                continue
            exclude_from_recall = self.record_border_patrol_candidate_unavailable_response(resp, beast_name)
            if exclude_from_recall is not None:
                remember_failed(patrol_failed_names, beast_name)
                if exclude_from_recall:
                    remember_failed(recall_failed_names, beast_name)
                await asyncio.sleep(3)
                continue
            return self.record_beast_border_patrol_response(resp, beast_name, mode)

    # ---- 灵兽：放养 ----

    def is_pasture_success(self, text):
        return bool(text and any(k in text for k in ["万兽奔腾", "万兽谷", "自动归来", "冲入"]))

    def is_no_resting_pasture_response(self, text):
        if not text: return False
        return "灵兽" in text and "放养" in text and "没有" in text and "休息中" in text

    def cn_num_to_int(self, value):
        """中文数字转整数"""
        value = str(value or "").strip()
        if not value: return 0
        if value.isdigit(): return int(value)
        nums = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if value == "十": return 10
        if "十" in value:
            left, _, right = value.partition("十")
            tens = nums.get(left, 1) if left else 1
            ones = nums.get(right, 0) if right else 0
            return tens * 10 + ones
        return nums.get(value, 0)

    def parse_pasture_dispatch_count(self, text):
        """从放养回复中解析放养数量"""
        if not text: return 0
        clean = text.replace("**", "").replace(" ", "")
        patterns = [r"放养(?:了)?([一二两三四五六七八九十\d]+)只",
                     r"等([一二两三四五六七八九十\d]+)只灵兽.*?(?:进入|冲入|放入|放养|奔向|前往|万兽谷)",
                     r"([一二两三四五六七八九十\d]+)只灵兽.*?(?:进入|冲入|放入|放养|奔向|前往|万兽谷)",
                     r"共([一二两三四五六七八九十\d]+)只.*?(?:放养|万兽谷)"]
        for pattern in patterns:
            m = re.search(pattern, clean)
            if m:
                count = self.cn_num_to_int(m.group(1))
                if count > 0: return count
        return 0

    def parse_pasture_return_count(self, text):
        """从放养归来回复中解析归来数量"""
        if not text: return 0
        clean = text.replace("**", "").replace(" ", "")
        for pattern in [r"放养的([一二两三四五六七八九十\d]+)只灵兽.*?归来",
                         r"等([一二两三四五六七八九十\d]+)只灵兽.*?归来",
                         r"([一二两三四五六七八九十\d]+)只灵兽已.*?归来"]:
            m = re.search(pattern, clean)
            if m:
                count = self.cn_num_to_int(m.group(1))
                if count > 0: return count
        return len(self.parse_pasture_return_names(text))

    def parse_pasture_return_names(self, text):
        """从归来回复中提取已归来的灵兽名"""
        if not text: return []
        clean = text.replace("**", "")
        names = []
        for name in re.findall(r"•\s*【([^】]+)】", clean):
            if name.strip(): names.append(name.strip())
        m = re.search(r"灵兽【([^】]+)】已.*?归来", clean)
        if m:
            for part in re.split(r"[、,，]", m.group(1)):
                part = re.sub(r"等[一二两三四五六七八九十\d]+只灵兽", "", part).strip()
                if part: names.append(part)
        deduped = []
        for name in names:
            if name not in deduped: deduped.append(name)
        return deduped

    def parse_pasture_return_stamina_recoveries(self, text):
        """从放养归来结算中解析每只灵兽恢复的体力。"""
        if not text:
            return {}
        clean = str(text or "").replace("**", "")
        recoveries = {}
        for match in re.finditer(r"【([^】]+)】[^\n\r]*?体力恢复\s*(\d+)", clean):
            name = match.group(1).strip()
            if self.is_valid_beast_name(name):
                recoveries[name] = int(match.group(2))
        return recoveries

    def is_pasture_return_message(self, text):
        """检测是否为放养归来消息"""
        if not text: return False
        clean = text.replace("**", "")
        return "放养" in clean and "灵兽" in clean and any(k in clean for k in ["归来", "自行归来", "一同归来", "清点收获", "灵兽归来"])

    def pasture_pending_count(self):
        try: return max(0, int(self.state.get("pasture_pending_count") or 0))
        except: return 0

    def pasture_returned_count(self):
        try: return max(0, int(self.state.get("pasture_returned_count") or 0))
        except: return 0

    def has_pending_pasture_return(self):
        pending = self.pasture_pending_count()
        return pending > 0 and self.pasture_returned_count() < pending

    def count_resting_beasts(self, cache=None):
        beasts = cache if cache is not None else self.state.get("beasts_cache", [])
        return sum(1 for beast in beasts if "休息" in (beast.get("status") or ""))

    def beast_name_matches(self, full_name, target_name):
        """灵兽名匹配（忽略括号后缀）"""
        full_name = str(full_name or "").strip(); target_name = str(target_name or "").strip()
        if not full_name or not target_name: return False
        base = re.sub(r"\s*\([^)]*\)", "", full_name).strip()
        return full_name == target_name or base == target_name

    def mark_resting_beasts_pastured(self, count=0):
        """标记休息中的灵兽为放养中状态"""
        changed = 0
        for beast in self.state.get("beasts_cache", []):
            if count and changed >= count: break
            if "休息" in (beast.get("status") or ""):
                beast["status"] = "放养中"; changed += 1
        if self.state.get("best_beast_status") and "休息" in self.state.get("best_beast_status"):
            best_name = self.state.get("best_beast_name", "")
            if any(b.get("full_name") == best_name and b.get("status") == "放养中" for b in self.state.get("beasts_cache", [])):
                self.state["best_beast_status"] = "放养中"
        return changed

    def mark_pastured_beasts_returned(self, text):
        """标记放养中的灵兽为已归来（休息中）"""
        names = self.parse_pasture_return_names(text)
        recoveries = self.parse_pasture_return_stamina_recoveries(text)
        changed = 0
        if names and any(self.beast_name_matches(name, BEAST_FOCUS_NAME) for name in names):
            self.state["focus_pasture_after_abyss_until"] = ""
        for beast in self.state.get("beasts_cache", []):
            if names and not any(self.beast_name_matches(beast.get("full_name"), name) for name in names): continue
            recovery = next((value for name, value in recoveries.items() if self.beast_name_matches(beast.get("full_name"), name)), None)
            if recovery is not None:
                current = self.beast_stamina_value(beast)
                beast["stamina"] = min(100, max(0, current) + recovery) if current >= 0 else recovery
            if names:
                if beast.get("status") != "休息中":
                    beast["status"] = "休息中"; changed += 1
            elif "放养" in (beast.get("status") or ""):
                beast["status"] = "休息中"; changed += 1
        if self.state.get("best_beast_status") and "放养" in self.state.get("best_beast_status"):
            best_name = self.state.get("best_beast_name", "")
            if not names or any(self.beast_name_matches(best_name, name) for name in names):
                self.state["best_beast_status"] = "休息中"
        best_name = self.state.get("best_beast_name", "")
        if best_name:
            for beast in self.state.get("beasts_cache", []):
                if self.beast_name_matches(beast.get("full_name", ""), best_name):
                    self.state["best_beast_stamina"] = self.beast_stamina_value(beast)
                    break
        return changed

    def mark_all_pastured_beasts_returned(self):
        """全部放养灵兽标记为已归来"""
        changed = 0
        protect_focus = self.focus_pasture_after_abyss_protected()
        for beast in self.state.get("beasts_cache", []):
            if protect_focus and self.beast_name_matches(beast.get("full_name", ""), BEAST_FOCUS_NAME):
                continue
            if "放养" in (beast.get("status") or ""): beast["status"] = "休息中"; changed += 1
        if self.state.get("best_beast_status") and "放养" in self.state.get("best_beast_status"):
            best_name = self.state.get("best_beast_name", "")
            if not (protect_focus and self.beast_name_matches(best_name, BEAST_FOCUS_NAME)):
                self.state["best_beast_status"] = "休息中"
        return changed

    def clear_pasture_pending(self):
        self.state["pasture_pending_count"] = 0
        self.state["pasture_returned_count"] = 0
        self.state["pasture_pending_since"] = ""

    def is_pasture_temporarily_blocked_response(self, text):
        clean = str(text or "").replace("**", "")
        if not any(k in clean for k in ["放养", "万兽谷", "灵兽", BEAST_FOCUS_NAME]):
            return False
        return any(k in clean for k in ["休养", "恢复", "暂时", "无法", "不能", "尚需", "还需", "请在", "后再", "冷却"])

    def focus_pasture_after_abyss_protected(self):
        until = self.state.get("focus_pasture_after_abyss_until", "")
        return bool(until and is_future(until))

    def focus_pasture_after_abyss_due(self):
        target = self.state.get("next_focus_pasture_after_abyss_time", "")
        return bool(target and not is_future(target))

    def focus_pasture_after_abyss_retry_seconds(self, response_text=""):
        retry = self.parse_injury_wait_time(response_text)
        if retry <= 0:
            retry = self.parse_wait_time(response_text)
        return retry if retry > 0 else FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS

    def set_next_pasture_not_after(self, target_time):
        if not target_time:
            return
        target_dt = str_to_dt(target_time) if isinstance(target_time, str) else target_time
        current = self.state.get("next_pasture_time", "")
        if not current or not is_future(current) or str_to_dt(current) > target_dt:
            self.state["next_pasture_time"] = dt_to_str(target_dt)

    def schedule_focus_pasture_after_abyss(self, beast_name=BEAST_FOCUS_NAME, response_text="", retry_seconds=None):
        if not self.beast_name_matches(beast_name, BEAST_FOCUS_NAME):
            return False
        retry = retry_seconds if retry_seconds is not None else self.focus_pasture_after_abyss_retry_seconds(response_text)
        retry = max(600, int(retry or FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS))
        target = add_seconds_str(now_str(), retry)
        self.state["next_focus_pasture_after_abyss_time"] = target
        self.set_next_pasture_not_after(target)
        self.save_state()
        log.info(f"Focus pasture after abyss scheduled at {target} (retry in {retry}s).")
        return True

    def record_focus_pasture_after_abyss_attempt(self, response_text, handled):
        if not self.state.get("next_focus_pasture_after_abyss_time"):
            return
        if handled and self.is_pasture_success(response_text):
            self.state["next_focus_pasture_after_abyss_time"] = ""
            self.state["focus_pasture_after_abyss_until"] = add_seconds_str(now_str(), PASTURE_CD_SECONDS)
            self.save_state()
            return
        if response_text and self.is_pasture_temporarily_blocked_response(response_text):
            self.schedule_focus_pasture_after_abyss(BEAST_FOCUS_NAME, response_text=response_text)
            return
        retry_at = self.state.get("next_pasture_time", "")
        if retry_at and is_future(retry_at):
            self.state["next_focus_pasture_after_abyss_time"] = retry_at
            self.save_state()
            return
        self.schedule_focus_pasture_after_abyss(BEAST_FOCUS_NAME, retry_seconds=FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS)

    def remember_manual_pasture_command_if_needed(self, msg, text):
        """记录手动发送的一键放养命令（用于同步状态）"""
        if (text or "").strip() != ".一键放养": return
        my_id = getattr(getattr(self, "my_info", None), "id", None)
        if not (getattr(msg, "out", False) or (my_id and getattr(msg, "sender_id", None) == my_id)): return
        self._manual_pasture_command_ids[getattr(msg, "id", None)] = time.monotonic()
        self._manual_pasture_command_ids = {mid: ts for mid, ts in self._manual_pasture_command_ids.items() if mid is not None and time.monotonic() - ts <= 300}

    def maybe_record_manual_pasture_dispatch(self, msg, text):
        """被动同步手动一键放养的派遣结果"""
        if not self.is_pasture_success(text): return False
        replied_id = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
        if replied_id not in self._manual_pasture_command_ids: return False
        now = now_str()
        self.state["last_pasture_time"] = now
        self.state["next_pasture_time"] = add_seconds_str(now, PASTURE_CD_SECONDS)
        self.record_pasture_dispatch(text, self.state.get("beasts_cache", []))
        self.save_state()
        return True

    def record_auto_pasture_response(self, f_resp, cache=None, best_name="", best_status="", block_actions=True):
        """解析自动 .一键放养 回复并更新放养状态。"""
        cache = cache if cache is not None else self.state.get("beasts_cache", [])
        if f_resp:
            f_cd = self.parse_wait_time(f_resp)
            if f_cd > 0 and self.is_pasture_temporarily_blocked_response(f_resp):
                retry = max(600, f_cd)
                self.schedule_pasture_retry(retry)
                log.info(f"Pasture temporarily blocked; retry in {retry}s.")
                return True
            if f_cd > 0 or self.is_pasture_success(f_resp):
                next_delay = (f_cd + PASTURE_RETURN_DELAY_SECONDS) if f_cd > 0 else PASTURE_CD_SECONDS
                self.state["last_pasture_time"] = add_seconds_str(now_str(), next_delay - PASTURE_CD_SECONDS)
                self.state["next_pasture_time"] = add_seconds_str(now_str(), next_delay)
                if self.is_pasture_success(f_resp):
                    self.record_pasture_dispatch(f_resp, cache, block_actions=block_actions)
                    if best_name:
                        self.mark_best_beast_pastured_if_needed(best_name, best_status, f_resp)
                self.save_state()
                return True
            if self.is_no_resting_pasture_response(f_resp):
                resting_count = self.count_resting_beasts(self.state.get("beasts_cache", []))
                if resting_count > 0:
                    self.state["last_pasture_time"] = now_str()
                    self.record_pasture_dispatch(f_resp, self.state.get("beasts_cache", []))
                    self.state["next_pasture_time"] = add_seconds_str(now_str(), PASTURE_CD_SECONDS)
                    log.info(f"Self-healed pasture state: {resting_count} resting beasts marked as pastured; set CD to 4 hours.")
                else:
                    pastured_count = sum(1 for beast in self.state.get("beasts_cache", []) if "放养" in (beast.get("status") or ""))
                    if pastured_count > 0 and not self.has_pending_pasture_return():
                        self.state["pasture_pending_count"] = pastured_count
                        self.state["pasture_returned_count"] = 0
                        self.state["pasture_pending_since"] = self.state.get("last_pasture_time") or now_str()
                    self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
                self.save_state()
                return True
            if self.is_pasture_temporarily_blocked_response(f_resp):
                self.schedule_pasture_retry(FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS)
                log.info(
                    f"Pasture temporarily blocked without parseable time; retry in "
                    f"{FOCUS_PASTURE_AFTER_ABYSS_RETRY_SECONDS}s."
                )
                return True
            notify_unrecognized_response(self, ".一键放养", f_resp, log, "一键放养")
            self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False
        self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
        self.save_state()
        return False

    def record_pasture_dispatch(self, response_text, fallback_cache=None, block_actions=True):
        """记录放养派遣信息：待归来计数和缓存标记"""
        count = self.parse_pasture_dispatch_count(response_text)
        if count <= 0: count = self.count_resting_beasts(fallback_cache)
        if count <= 0 and self.is_pasture_success(response_text): count = 1
        self.state["pasture_pending_count"] = count
        self.state["pasture_returned_count"] = 0
        self.state["pasture_pending_since"] = now_str()
        self.state["last_pasture_return_time"] = ""
        marked = self.mark_resting_beasts_pastured(count)
        log.info(f"Pasture dispatch recorded: pending={count}, cache_marked={marked}.")
        best_name = self.state.get("best_beast_name", "")
        if block_actions and best_name and self.is_pastured_status(self.state.get("best_beast_status", "")):
            self.defer_beast_actions_while_pastured(best_name, "pasture dispatch")

    async def handle_pasture_return_event(self, event, text=None, sender=None):
        """处理放养归来事件（被动接收，实时更新归来计数）"""
        msg = event.message
        text = text if text is not None else (msg.text or "")
        sender = sender or await event.get_sender()
        if not self.is_pasture_return_message(text): return False
        sender_name = (getattr(sender, "username", "") or getattr(sender, "first_name", "") or "").strip()
        if sender and not is_game_bot_sender(self, sender):
            log.info(f"Pasture return ignored from non-game sender {sender_name or 'unknown'}: {text[:120]}")
            return False
        if not self.text_targets_self(msg, text):
            log.info(f"Pasture return ignored because it does not target this account: {text[:160]}")
            return False
        returned = self.parse_pasture_return_count(text)
        if returned <= 0:
            log.info(f"Pasture return detected but no beast count parsed: {text[:160]}")
            return True
        msg_id = getattr(msg, "id", None)
        prev_for_msg = self._pasture_return_seen_counts.get(msg_id, 0) if msg_id is not None else 0
        if msg_id is not None and returned <= prev_for_msg: return True
        if msg_id is not None:
            self._pasture_return_seen_counts[msg_id] = returned
            if len(self._pasture_return_seen_counts) > 300: self._pasture_return_seen_counts = dict(list(self._pasture_return_seen_counts.items())[-150:])
        delta = returned - prev_for_msg if msg_id is not None else returned
        pending = self.pasture_pending_count(); current_returned = self.pasture_returned_count()
        if pending > 0: self.state["pasture_returned_count"] = min(pending, current_returned + delta)
        else: self.state["pasture_returned_count"] = max(current_returned, returned)
        self.state["last_pasture_return_time"] = now_str()
        changed = self.mark_pastured_beasts_returned(text)
        pending = self.pasture_pending_count(); returned_total = self.pasture_returned_count()
        if pending <= 0 or returned_total >= pending:
            extra_changed = self.mark_all_pastured_beasts_returned()
            if extra_changed: log.info(f"Pasture return complete: normalized {extra_changed} cached beasts.")
            self.clear_pasture_pending()
            self.state["next_pasture_time"] = add_seconds_str(now_str(), PASTURE_RETURN_DELAY_SECONDS)
        else: self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
        self.wake_overdue_beast_border_patrol("pasture return")
        self.save_state()
        self.beast_wakeup.set()
        log.info(
            f"Pasture return recorded: returned={returned}, changed={changed}, "
            f"pending={self.pasture_pending_count()}, next_patrol={self.state.get('next_beast_border_patrol_time', '')}."
        )
        return True

    async def sleep_beast_action(self, sleep_for):
        """可被 beast_wakeup 中断的睡眠（放养归来时唤醒）"""
        try: await asyncio.wait_for(self.beast_wakeup.wait(), timeout=max(1, sleep_for))
        except asyncio.TimeoutError: return
        finally: self.beast_wakeup.clear()

    def is_abyss_busy_response(self, text):
        """检测灵兽是否正忙（无法探渊）"""
        return bool(text and ("正忙" in text or "无法进入万兽渊" in text))

    def is_beast_deploy_success(self, text):
        """检测灵兽出战是否成功"""
        return bool(text and any(k in text for k in ["设为出战", "出战状态", "并肩作战", "已经出战", "已出战"]))

    def is_focus_beast_resting_for_pasture_response(self, text):
        """检测六翼是否已经回到休息状态，可以参与一键放养。"""
        if not text:
            return False
        rest_status = self.parse_rest_response_status(text)
        if rest_status and "休息" in rest_status:
            return True
        return BEAST_FOCUS_NAME in text and "休息中" in text and not self.is_beast_injury_response(text)

    def schedule_pasture_retry(self, retry_seconds=600):
        self.state["next_pasture_time"] = add_seconds_str(now_str(), retry_seconds)
        self.save_state()

    async def ensure_focus_beast_ready_for_pasture(self):
        """一键放养前不强制六翼出战；如六翼出战则先召回休息。"""
        focus = self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
        if not focus:
            state_focus_name = self.state.get("best_beast_name", "")
            if self.beast_name_matches(state_focus_name, BEAST_FOCUS_NAME):
                focus = {
                    "full_name": state_focus_name or BEAST_FOCUS_NAME,
                    "status": self.state.get("best_beast_status", ""),
                }
            else:
                log.info(f"Pasture precheck: {BEAST_FOCUS_NAME} not in cache; sending .一键放养 without forced deploy.")
                return True

        status = (focus or {}).get("status", "")
        focus_cache_name = (focus or {}).get("full_name") or BEAST_FOCUS_NAME
        if self.is_pastured_status(status):
            log.info(f"Pasture precheck: {focus_cache_name} already pastured; .一键放养 can still handle other resting beasts.")
            return True
        if "出战" not in status:
            return True

        log.info(f"Pasture precheck: {BEAST_FOCUS_NAME} is deployed; resting before .一键放养.")
        rest_status, rest_resp = await self.rest_beast_for_abyss(focus_cache_name)
        if "休息" in str(rest_status or "") or self.is_focus_beast_resting_for_pasture_response(rest_resp):
            self.set_best_beast_status(focus_cache_name, "休息中")
            return True
        if self.is_beast_pastured_response(rest_resp, focus_cache_name) or self.is_pastured_status(rest_status):
            log.info(f"Pasture precheck: {focus_cache_name} is already pastured after rest attempt.")
            return True

        injury_cd = self.record_beast_injury_from_response(focus_cache_name, rest_resp, source="pasture")
        if injury_cd >= 0:
            retry_seconds = max(600, injury_cd)
            self.schedule_pasture_retry(retry_seconds)
            log.warning(f"Pasture deferred: {BEAST_FOCUS_NAME} cannot rest while injured; retry in {retry_seconds}s.")
            return False

        if rest_resp:
            notify_unrecognized_response(self, f"万兽谷灵兽休息 {BEAST_FOCUS_NAME}", rest_resp, log, "一键放养前休息")
        self.schedule_pasture_retry()
        return False

    async def execute_focus_low_stamina_pasture(self, cache=None):
        """六翼体力低于保护线时，先让六翼休息并执行一键放养。"""
        cache = list(cache or self.state.get("beasts_cache", []))
        focus = None
        for beast in cache:
            if self.beast_name_matches(beast.get("full_name", ""), BEAST_FOCUS_NAME):
                focus = beast
                break
        focus = focus or self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
        if not focus:
            log.info(f"Focus pasture skipped: {BEAST_FOCUS_NAME} not found in beast cache.")
            self.schedule_pasture_retry()
            return False

        stamina = self.beast_stamina_value(focus)
        if stamina < 0 or stamina >= BEAST_FOCUS_PROTECT_STAMINA:
            return False

        focus_name = focus.get("full_name") or BEAST_FOCUS_NAME
        status = focus.get("status", "未知")
        if self.is_pastured_status(status):
            self.defer_focus_beast_personal_actions_while_pastured("low stamina precheck")
            return True
        if self.is_injury_status(status):
            retry = focus.get("status_cd", -1)
            self.schedule_pasture_retry(retry if retry and retry > 0 else BEAST_ACTION_RETRY_SECONDS)
            log.info(f"Focus pasture deferred: {focus_name} status is {status}.")
            return False
        if any(k in status for k in ["探险", "偷菜", "巡游"]):
            retry = focus.get("status_cd", -1)
            self.schedule_pasture_retry(retry if retry and retry > 0 else BEAST_ACTION_RETRY_SECONDS)
            log.info(f"Focus pasture deferred: {focus_name} status is {status}.")
            return False

        if "休息" not in status:
            log.info(f"Focus pasture: resting {focus_name} before .一键放养 (status={status or '未知'}, stamina={stamina}).")
            rest_status, rest_resp = await self.rest_beast_for_abyss(focus_name)
            if self.is_beast_pastured_response(rest_resp, focus_name) or self.is_pastured_status(rest_status):
                self.defer_focus_beast_personal_actions_while_pastured("low stamina rest response")
                return True
            if rest_status and "休息" in rest_status:
                status = rest_status
            else:
                injury_cd = self.record_beast_injury_from_response(focus_name, rest_resp, source="pasture")
                retry = injury_cd if injury_cd > 0 else BEAST_ACTION_RETRY_SECONDS
                self.schedule_pasture_retry(retry)
                if rest_resp and injury_cd < 0 and not self.is_fake_beast_status_response(rest_resp):
                    notify_unrecognized_response(self, f"万兽谷灵兽休息 {focus_name}", rest_resp, log, "低体力放养前休息")
                return False
            await asyncio.sleep(3)

        log.info(f"Focus pasture: sending .一键放养 for {focus_name} (stamina={stamina}).")
        f_resp = await self.send_and_wait_feedback(".一键放养")
        return self.record_auto_pasture_response(f_resp, cache, focus_name, status, block_actions=False)

    def queue_focus_pasture_after_priority_action(self, beast_name, action="priority action"):
        """标记六翼完成探渊/偷菜，等待立即放养恢复体力。"""
        if not self.beast_name_matches(beast_name, BEAST_FOCUS_NAME):
            return False
        self.state["next_focus_pasture_after_abyss_time"] = now_str()
        self.set_next_pasture_not_after(now_str())
        self.save_state()
        log.info(f"Focus pasture queued after {action}: {BEAST_FOCUS_NAME}.")
        return True

    async def attempt_focus_pasture_after_priority_action(self, beast_name, action="priority action"):
        """六翼完成探渊/偷菜后立刻尝试放养；若仍需休养则由回执重排。"""
        if not self.queue_focus_pasture_after_priority_action(beast_name, action):
            return False
        if not await self.ensure_focus_beast_ready_for_pasture():
            self.schedule_focus_pasture_after_abyss(
                BEAST_FOCUS_NAME,
                retry_seconds=BEAST_ACTION_RETRY_SECONDS,
            )
            return False
        log.info(f"Focus pasture after {action}: trying .一键放养 immediately for {BEAST_FOCUS_NAME}.")
        f_resp = await self.send_and_wait_feedback(".一键放养")
        handled = self.record_auto_pasture_response(
            f_resp,
            self.state.get("beasts_cache", []),
            BEAST_FOCUS_NAME,
            "休息中",
            block_actions=False,
        )
        self.record_focus_pasture_after_abyss_attempt(f_resp, handled)
        return handled

    async def attempt_focus_pasture_after_abyss(self, beast_name):
        return await self.attempt_focus_pasture_after_priority_action(beast_name, "abyss")

    def is_no_beast_deployed_for_steal_response(self, text):
        """检测偷菜时未出战灵兽的明确失败回复"""
        clean = str(text or "")
        return "尚未派遣任何灵兽出战" in clean or ("无法执行此任务" in clean and "灵兽出战" in clean)

    def is_steal_accepted_response(self, text):
        """检测灵兽偷菜已受理或已结算的正常回复。"""
        clean = str(text or "")
        return any(k in clean for k in [
            "成功", "获得", "偷菜", "已领命", "潜行",
            "已锁定目标", "正在准备动手",
        ])

    def is_steal_pending_response(self, text):
        """偷菜已受理但尚未结算，此时不能提前放养执行灵兽。"""
        clean = str(text or "")
        return any(k in clean for k in ["已领命", "潜行", "已锁定目标", "正在准备动手"])

    def is_steal_settled_response(self, text):
        """偷菜已经产生最终成功/收益回执，可以放养恢复。"""
        clean = str(text or "")
        if not clean or self.is_steal_pending_response(clean):
            return False
        return any(k in clean for k in ["偷菜成功", "获得", "收获", "带回", "战利品", "偷得"])

    def handle_beast_deploy_failure_for_steal(self, beast_name, response_text, context):
        """Handle explicit deploy failures before steal without emitting unknown alerts."""
        if not response_text:
            self.set_next_steal_not_before(add_seconds_str(now_str(), 1800))
            self.save_state()
            return False
        if self.is_beast_pastured_response(response_text, beast_name):
            self.defer_beast_actions_while_pastured(beast_name, context)
            return True
        if self.handle_no_such_beast_response(beast_name, response_text, context):
            return True
        injury_cd = self.record_beast_injury_from_response(beast_name, response_text, source="steal")
        if injury_cd >= 0:
            self.set_next_steal_not_before(add_seconds_str(now_str(), max(1800, injury_cd)))
            self.save_state()
            log.info(
                f"Steal candidate skipped: {beast_name} cannot deploy while injured; "
                f"next steal not before {self.state.get('next_steal_time', '')} ({context})."
            )
            return True
        return False

    async def execute_steal_with_candidate(self, cache=None):
        """偷菜首选六翼；候选受伤/体力不足/忙碌时继续补位。"""
        steal_cache = list(self.state.get("beasts_cache", [])) or list(cache or [])
        candidates = self.steal_candidate_beasts(steal_cache)
        if not candidates:
            status_check = self.state.get("next_beast_status_check_time", "")
            retry_at = status_check if status_check and is_future(status_check) else add_seconds_str(now_str(), 1800)
            log.info(f"Steal: no suitable cached beast candidate; next attempt not before {retry_at}.")
            self.set_next_steal_not_before(retry_at)
            self.save_state()
            return False

        last_response = ""
        for best in candidates:
            best_name = best.get("full_name", "")
            if not best_name:
                continue
            self.update_best_beast_tracking(best)
            self.save_state()
            best_status = self.state.get("best_beast_status") or best.get("status", "未知")
            if not self.can_attempt_steal_status(best_status):
                log.info(f"Steal candidate skipped: {best_name} status is {best_status}.")
                continue

            if self.is_pastured_status(best_status):
                ok, recall_resp = await self.recall_pastured_beast_for_action(best_name, "steal")
                last_response = recall_resp or last_response
                if not ok:
                    continue
                best_status = "休息中"
                await asyncio.sleep(3)

            deploy_ok = best_status == "出战中"
            if best_status != "出战中":
                deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                last_response = deploy_resp or last_response
                if self.is_beast_deploy_success(deploy_resp):
                    self.set_best_beast_status(best_name, "出战中")
                    deploy_ok = True
                elif deploy_resp and self.is_fake_beast_status_response(deploy_resp):
                    deploy_ok = await self.normalize_beast_for_steal(best_name)
                elif deploy_resp and self.is_beast_pastured_response(deploy_resp, best_name):
                    self.set_best_beast_status(best_name, "放养中")
                    ok, recall_resp = await self.recall_pastured_beast_for_action(best_name, "steal")
                    last_response = recall_resp or last_response
                    if ok:
                        await asyncio.sleep(3)
                        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                        last_response = deploy_resp or last_response
                        if self.is_beast_deploy_success(deploy_resp):
                            self.set_best_beast_status(best_name, "出战中")
                            deploy_ok = True
                        else:
                            deploy_ok = False
                    else:
                        deploy_ok = False
                else:
                    if self.handle_no_such_beast_response(best_name, deploy_resp, "偷菜前出战"):
                        log.info(f"Steal candidate skipped: {best_name} no longer exists in beast roster.")
                    else:
                        injury_cd = self.record_beast_injury_from_response(best_name, deploy_resp, source="steal")
                        if injury_cd >= 0:
                            log.info(f"Steal candidate skipped: {best_name} injured for {injury_cd}s.")
                        elif deploy_resp:
                            notify_unrecognized_response(self, f".灵兽出战 {best_name}", deploy_resp, log, "偷菜前出战")
                    deploy_ok = False
                await asyncio.sleep(3)

            if not deploy_ok:
                continue

            s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
            last_response = s_resp or last_response
            if s_resp and self.is_fake_beast_status_response(s_resp):
                if await self.normalize_beast_for_steal(best_name):
                    await asyncio.sleep(3)
                    s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
                    last_response = s_resp or last_response
                else:
                    s_resp = ""

            if s_resp and self.is_no_beast_deployed_for_steal_response(s_resp):
                deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                last_response = deploy_resp or last_response
                retry_ok = False
                if self.is_beast_deploy_success(deploy_resp):
                    self.set_best_beast_status(best_name, "出战中")
                    retry_ok = True
                elif deploy_resp and self.is_fake_beast_status_response(deploy_resp):
                    retry_ok = await self.normalize_beast_for_steal(best_name)
                elif deploy_resp and self.is_beast_pastured_response(deploy_resp, best_name):
                    self.set_best_beast_status(best_name, "放养中")
                    ok, recall_resp = await self.recall_pastured_beast_for_action(best_name, "steal")
                    last_response = recall_resp or last_response
                    if ok:
                        await asyncio.sleep(3)
                        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                        last_response = deploy_resp or last_response
                        retry_ok = self.is_beast_deploy_success(deploy_resp)
                        if retry_ok:
                            self.set_best_beast_status(best_name, "出战中")
                else:
                    if self.handle_no_such_beast_response(best_name, deploy_resp, "偷菜重试出战"):
                        log.info(f"Steal retry skipped: {best_name} no longer exists in beast roster.")
                    else:
                        injury_cd = self.record_beast_injury_from_response(best_name, deploy_resp, source="steal")
                        if injury_cd >= 0:
                            log.info(f"Steal candidate skipped on retry: {best_name} injured for {injury_cd}s.")
                        elif deploy_resp:
                            notify_unrecognized_response(self, f".灵兽出战 {best_name}", deploy_resp, log, "偷菜重试出战")
                if retry_ok:
                    await asyncio.sleep(3)
                    s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
                    last_response = s_resp or last_response
                else:
                    s_resp = ""

            if not s_resp:
                continue
            if self.is_beast_pastured_response(s_resp, best_name):
                self.set_best_beast_status(best_name, "放养中")
                log.info(f"Steal candidate skipped: {best_name} became pastured.")
                continue
            if self.is_beast_stamina_insufficient_response(s_resp):
                self.record_beast_stamina_shortage(best_name, s_resp, "steal")
                await asyncio.sleep(3)
                continue
            injury_cd = self.record_beast_injury_from_response(best_name, s_resp, source="steal")
            if injury_cd >= 0:
                log.info(f"Steal candidate skipped: {best_name} injured during steal for {injury_cd}s.")
                await asyncio.sleep(3)
                continue

            s_cd = self.parse_wait_time(s_resp)
            if s_cd > 0:
                self.state["last_steal_time"] = add_seconds_str(now_str(), s_cd - 14400)
                self.state["next_steal_time"] = add_seconds_str(now_str(), s_cd)
            elif self.is_steal_accepted_response(s_resp):
                self.state["last_steal_time"] = now_str()
                self.state["next_steal_time"] = add_seconds_str(now_str(), 14400)
                self.set_best_beast_status(
                    best_name,
                    "偷菜中" if self.is_steal_pending_response(s_resp) else "出战中",
                )
            else:
                notify_unrecognized_response(self, ".灵兽偷菜", s_resp, log, f"灵兽偷菜[{best_name}]")
                continue
            self.save_state()
            if self.beast_name_matches(best_name, BEAST_FOCUS_NAME):
                if self.is_steal_pending_response(s_resp):
                    self.schedule_focus_pasture_after_abyss(
                        best_name,
                        retry_seconds=BEAST_ACTION_RETRY_SECONDS,
                    )
                    log.info(f"Focus pasture deferred: {BEAST_FOCUS_NAME} is still stealing.")
                else:
                    await self.attempt_focus_pasture_after_priority_action(best_name, "steal")
            return True

        status_check = self.state.get("next_beast_status_check_time", "")
        retry_at = status_check if status_check and is_future(status_check) else add_seconds_str(now_str(), 1800)
        log.warning(f"Steal: all candidates failed or unavailable. Last response: {last_response[:80]}")
        self.set_next_steal_not_before(retry_at)
        self.save_state()
        return False

    def is_abyss_success_response(self, text):
        """检测探渊是否成功"""
        return bool(text and not self.is_abyss_busy_response(text) and any(k in text for k in ["成功", "出发", "进入", "送入", "历练", "击败", "战利品", "带回"]))

    def parse_rest_response_status(self, text):
        """解析灵兽休息后的状态"""
        if not text: return ""
        clean = str(text).replace("**", "")
        # 放养中的灵兽被提前召回时，机器人会同时写“召回”和“还需
        # ...后自行归来”。这不是休息成功，不能让后续巡边/探渊立刻接管。
        if (
            "放养中" in clean
            and ("提前召回" in clean or "自行归来" in clean or "不会结算" in clean)
        ):
            return "放养中"
        m = re.search(r"正在[（(]([^）)]+)[）)]", clean)
        if m: return m.group(1).strip()
        if any(k in clean for k in ["召回", "休养", "休息"]): return "休息中"
        return ""

    def set_next_abyss_not_before(self, target_time):
        """设置下次探渊不早于目标时间（合并多个条件取最大值）"""
        if not target_time: return
        target_dt = str_to_dt(target_time) if isinstance(target_time, str) else target_time
        candidates = [target_dt]
        next_abyss = self.state.get("next_abyss_time", "")
        if next_abyss: candidates.append(str_to_dt(next_abyss))
        last_abyss = self.state.get("last_abyss_time", "")
        if last_abyss: candidates.append(str_to_dt(last_abyss) + timedelta(seconds=21600))
        chosen = max(candidates)
        self.state["next_abyss_time"] = dt_to_str(chosen)

    def set_next_steal_not_before(self, target_time):
        """设置下次偷菜不早于目标时间"""
        if not target_time: return
        target_dt = str_to_dt(target_time) if isinstance(target_time, str) else target_time
        candidates = [target_dt]
        next_steal = self.state.get("next_steal_time", "")
        if next_steal: candidates.append(str_to_dt(next_steal))
        last_steal = self.state.get("last_steal_time", "")
        if last_steal: candidates.append(str_to_dt(last_steal) + timedelta(seconds=14400))
        chosen = max(candidates)
        self.state["next_steal_time"] = dt_to_str(chosen)

    def schedule_abyss_retry(self, retry_seconds=None):
        """安排探渊重试时间"""
        if retry_seconds is None:
            status_check = self.state.get("next_beast_status_check_time", "")
            retry_seconds = int(seconds_until(status_check)) if status_check and is_future(status_check) else 600
            retry_seconds = max(60, retry_seconds)
        self.set_next_abyss_not_before(add_seconds_str(now_str(), retry_seconds))
        self.save_state()

    def beast_action_due(self, next_key, last_key, cooldown_seconds):
        """判断灵兽指令是否到期，显式 next_* 时间优先于历史 last_* 兜底。"""
        next_time = self.state.get(next_key, "")
        if next_time and is_future(next_time):
            return False
        last_time = self.state.get(last_key, "")
        if last_time and is_future(add_seconds_str(last_time, cooldown_seconds)):
            return False
        return True

    def beast_action_wait_seconds(self, next_key, last_key, cooldown_seconds):
        """计算灵兽指令下一次可执行等待时间。"""
        waits = []
        next_time = self.state.get(next_key, "")
        if next_time and is_future(next_time):
            waits.append(seconds_until(next_time))
        last_time = self.state.get(last_key, "")
        if last_time:
            fallback_next = add_seconds_str(last_time, cooldown_seconds)
            if is_future(fallback_next):
                waits.append(seconds_until(fallback_next))
        return min(waits) if waits else 0

    def all_cached_beasts_pastured(self, cache=None):
        cache = list(cache if cache is not None else self.state.get("beasts_cache", []))
        return bool(cache) and all(self.is_pastured_status(beast.get("status", "")) for beast in cache)

    def record_beast_injury_from_response(self, beast_name, text, source=""):
        """记录灵兽受伤及恢复时间"""
        if not self.is_beast_injury_response(text): return -1
        parsed_cd = self.parse_injury_wait_time(text)
        injury_cd = parsed_cd if parsed_cd > 0 else BEAST_INJURY_DEFAULT_RETRY_SECONDS
        source = source or ""
        injury_status = "重伤" if source == "abyss" and "重伤" in text else "受伤"
        self.state["best_beast_injury_source"] = source
        self.set_best_beast_status(beast_name, injury_status, injury_cd)
        self.record_best_beast_status_timing(injury_status, injury_cd)
        if source == "abyss":
            self.schedule_abyss_retry(injury_cd)
        return injury_cd

    def defer_beast_action_after_injury(self, action, injury_cd):
        """受伤后推迟灵兽操作"""
        status_check = self.state.get("next_beast_status_check_time", "")
        if injury_cd > 0: target_time = add_seconds_str(now_str(), injury_cd)
        elif status_check and is_future(status_check): target_time = status_check
        else: target_time = add_seconds_str(now_str(), 600)
        if action == "abyss": self.set_next_abyss_not_before(target_time)
        elif action == "steal": self.set_next_steal_not_before(target_time)
        self.save_state()

    async def rest_beast_for_abyss(self, beast_name):
        """通过万兽谷按灵兽 ID 召回休息，绝不回退群指令。"""
        contract = getattr(self, "_miniapp_beast_contract", None)
        transport = getattr(contract, "transport", None)
        if transport is None:
            message = "万兽谷 Mini App 未配置，无法执行灵兽休息"
            log.error(message)
            return "", message

        beast = self.get_cached_beast_by_name(beast_name)
        try:
            beast_id = int((beast or {}).get("id") or 0)
        except (TypeError, ValueError):
            beast_id = 0
        try:
            if beast_id <= 0:
                snapshot = await transport.spirit_beast_snapshot("主魂", log_operation=False)
                beasts = list((snapshot or {}).get("beasts") or [])
                if beasts:
                    self.state["beasts_cache"] = beasts
                    self.update_best_beast_tracking()
                    self.save_state()
                beast = next(
                    (
                        item for item in beasts
                        if self.beast_name_matches(item.get("full_name", ""), beast_name)
                    ),
                    None,
                )
                beast_id = int((beast or {}).get("id") or 0)
            if beast_id <= 0:
                message = f"万兽谷未找到灵兽【{beast_name}】"
                log.warning(message)
                return "", message

            result = await transport.spirit_beast_rest("主魂", beast_id, beast_name)
            beasts = list((result or {}).get("beasts") or [])
            if beasts:
                self.state["beasts_cache"] = beasts
                self.update_best_beast_tracking()
            updated = next(
                (item for item in beasts if int(item.get("id") or 0) == beast_id),
                None,
            )
            rest_status = str((updated or {}).get("status") or "").strip()
            rest_resp = str((result or {}).get("message") or "万兽谷灵兽休息完成").strip()
            if "休息" in rest_status:
                self.set_best_beast_status(beast_name, "休息中")
                self.save_state()
                return "休息中", rest_resp
            self.save_state()
            return rest_status, rest_resp
        except asyncio.CancelledError:
            raise
        except MiniAppBeastError as exc:
            message = f"万兽谷灵兽休息失败：{exc.code}"
            log.error("Wan Beast Valley rest failed for %s: %s", beast_name, exc.code)
            return "", message
        except Exception as exc:
            message = f"万兽谷灵兽休息失败：{type(exc).__name__}"
            log.exception("Wan Beast Valley rest failed for %s", beast_name)
            return "", message

    async def recall_pastured_beast_for_action(self, beast_name, action=""):
        """放养中的灵兽通过万兽谷召回后继续执行任务。"""
        action_label = action or "action"
        log.info(f"Beast {action_label}: {beast_name} is pastured; recalling in Wan Beast Valley.")
        rest_status, rest_resp = await self.rest_beast_for_abyss(beast_name)
        if rest_status and "休息" in rest_status:
            self.set_best_beast_status(beast_name, "休息中")
            return True, rest_resp
        if self.handle_no_such_beast_response(beast_name, rest_resp, f"{action_label} recall"):
            return False, rest_resp
        injury_cd = self.record_beast_injury_from_response(beast_name, rest_resp, source=action_label)
        if injury_cd >= 0:
            log.info(f"Beast {action_label}: {beast_name} cannot be recalled while injured; retry after {injury_cd}s.")
            return False, rest_resp
        if self.is_beast_pastured_response(rest_resp, beast_name) or self.is_pastured_status(rest_status):
            self.set_best_beast_status(beast_name, "放养中")
            log.info(f"Beast {action_label}: {beast_name} is still pastured after recall attempt.")
            return False, rest_resp
        if rest_resp and not self.is_fake_beast_status_response(rest_resp):
            notify_unrecognized_response(self, f"万兽谷灵兽休息 {beast_name}", rest_resp, log, f"灵兽{action_label}前召回")
        return False, rest_resp

    async def normalize_beast_for_steal(self, beast_name):
        """修复偷菜时的伪受伤状态（切换出战状态）"""
        log.warning(f"Beast stale/fake status detected for steal. Switching {beast_name} to battle once.")
        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {beast_name}", timeout=60, max_retries=1)
        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(beast_name, "出战中"); return True
        if self.handle_beast_deploy_failure_for_steal(beast_name, deploy_resp, "偷菜假状态切换"): return False
        if deploy_resp and not self.is_fake_beast_status_response(deploy_resp): notify_unrecognized_response(self, f".灵兽出战 {beast_name}", deploy_resp, log, "偷菜假状态切换")
        self.set_next_steal_not_before(add_seconds_str(now_str(), 1800)); self.save_state(); return False

    async def normalize_beast_for_abyss(self, beast_name):
        """
        修复探渊时的伪受伤状态（toggle 出战→休息）。
        先发 .灵兽出战 切换状态，再通过万兽谷回到休息中。
        这种 toggle 刷新了灵兽的实际状态，清除"伪受伤"。
        """
        log.warning(f"Beast stale/fake status detected for abyss. Toggling {beast_name} battle/rest once.")
        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {beast_name}", timeout=60, max_retries=1)
        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(beast_name, "出战中")
        elif self.is_beast_pastured_response(deploy_resp, beast_name):
            ok, _ = await self.recall_pastured_beast_for_action(beast_name, "abyss")
            return ok
        elif self.handle_no_such_beast_response(beast_name, deploy_resp, "探渊假状态出战"):
            self.schedule_abyss_retry(1800)
            return False
        elif deploy_resp and not self.is_fake_beast_status_response(deploy_resp):
            injury_cd = self.record_beast_injury_from_response(beast_name, deploy_resp, source="abyss")
            if injury_cd >= 0: self.defer_beast_action_after_injury("abyss", injury_cd); return False
            notify_unrecognized_response(self, f".灵兽出战 {beast_name}", deploy_resp, log, "探渊假状态出战"); self.schedule_abyss_retry(1800); return False
        await asyncio.sleep(3)
        rest_status, rest_resp = await self.rest_beast_for_abyss(beast_name)
        if rest_status == "休息中": return True
        if self.is_beast_pastured_response(rest_resp, beast_name) or self.is_pastured_status(rest_status):
            self.defer_beast_actions_while_pastured(beast_name, "abyss normalize rest response")
            return False
        if self.handle_no_such_beast_response(beast_name, rest_resp, "探渊假状态休息"):
            self.schedule_abyss_retry(1800)
            return False
        if rest_resp and not self.is_fake_beast_status_response(rest_resp): notify_unrecognized_response(self, f"万兽谷灵兽休息 {beast_name}", rest_resp, log, "探渊假状态休息")
        self.schedule_abyss_retry(1800); return False

    def mark_best_beast_pastured_if_needed(self, best_name, best_status, response_text):
        """需要时标记最佳灵兽为放养中"""
        if not best_name or not self.is_pasture_success(response_text): return best_status
        if "放养" in (best_status or ""): return best_status
        if best_name in response_text or best_status in ("休息中", "未知", ""):
            self.set_best_beast_status(best_name, "放养中"); return "放养中"
        return best_status

    def select_release_beast(self, cache):
        """选择要放生的灵兽：优先放生品种重复中战力最低的"""
        indexed = [(idx, beast) for idx, beast in enumerate(cache)]
        groups = {}
        for idx, beast in indexed:
            species = beast.get("species") or beast.get("type") or beast.get("full_name", "")
            groups.setdefault(species, []).append((idx, beast))
        duplicate_candidates = []
        for members in groups.values():
            if len(members) <= 1: continue
            duplicate_candidates.append(min(members, key=lambda item: (item[1].get("power", 0), item[0])))
        candidates = duplicate_candidates or indexed
        return min(candidates, key=lambda item: (item[1].get("power", 0), item[0]))[1]

    def is_wind_sparrow(self, beast_or_text):
        """检测是否为风雀（触发停止狩猎的条件）"""
        if isinstance(beast_or_text, dict):
            species = str(beast_or_text.get("species") or beast_or_text.get("type") or "")
            species = re.sub(r"^[一二三四五六七八九十\d]+阶", "", species).strip()
            return species == "风雀"
        return "风雀" in str(beast_or_text or "")

    def tenth_beast(self, cache=None):
        cache = list(cache if cache is not None else self.state.get("beasts_cache", []))
        return cache[9] if len(cache) >= 10 else None

    def stop_beast_hunt(self, reason):
        """永久停止灵兽狩猎"""
        self.state["beast_hunt_stopped"] = True
        self.state["beast_hunt_stopped_reason"] = reason
        self.state["next_hunt_time"] = ""
        self.save_state()

    def should_stop_hunt_by_tenth_beast(self, cache=None):
        """检查第10只灵兽是否为风雀（风雀不值得继续抓）"""
        tenth = self.tenth_beast(cache)
        if tenth and self.is_wind_sparrow(tenth): self.stop_beast_hunt(f"第十只灵兽种类是风雀：{tenth.get('full_name', '风雀')}"); return True
        return False

    async def send_abyss_once(self, beast_name):
        """发送一次探渊指令并获取结果（可能需要等编辑）"""
        resp_msg = await self.send_and_wait_feedback(f".探渊 {beast_name}", timeout=60, return_response_msg=True)
        if not resp_msg: return ""
        text = resp_msg.text or ""
        if self.is_beast_stamina_insufficient_response(text) or self.is_abyss_busy_response(text) or self.parse_wait_time(text) > 0:
            return text
        await asyncio.sleep(25)
        try:
            latest = await self.client.get_messages(self.target_chat_id, ids=resp_msg.id)
            if latest and latest.text: return latest.text
        except Exception as e: log.warning(f"Abyss: failed to fetch edited result for {resp_msg.id}: {e}")
        return text

    async def send_abyss_with_busy_retry(self, beast_name):
        """发送探渊指令，遇正忙时自动修复后重试一次"""
        resp = await self.send_abyss_once(beast_name)
        if self.is_beast_pastured_response(resp, beast_name):
            ok, recall_resp = await self.recall_pastured_beast_for_action(beast_name, "abyss")
            if ok:
                await asyncio.sleep(3)
                return await self.send_abyss_once(beast_name)
            self.schedule_abyss_retry(1800)
            return recall_resp or resp
        if self.is_abyss_busy_response(resp):
            cached = self.get_cached_beast_by_name(beast_name)
            if self.is_pastured_status((cached or {}).get("status", "")) or self.is_pastured_status(self.state.get("best_beast_status", "")):
                ok, recall_resp = await self.recall_pastured_beast_for_action(beast_name, "abyss")
                if ok:
                    await asyncio.sleep(3)
                    return await self.send_abyss_once(beast_name)
                self.schedule_abyss_retry(1800)
                return recall_resp or resp
            log.warning(f"Abyss blocked by stale busy status for {beast_name}. Normalizing once before retry.")
            if not await self.normalize_beast_for_abyss(beast_name): return resp
            await asyncio.sleep(3)
            resp = await self.send_abyss_once(beast_name)
            if self.is_beast_pastured_response(resp, beast_name):
                self.defer_beast_actions_while_pastured(beast_name, "abyss retry response")
            elif self.is_abyss_busy_response(resp): log.info(f"Abyss still blocked by busy status for {beast_name}; retry later."); self.schedule_abyss_retry(1800)
        return resp

    def is_beast_stamina_insufficient_response(self, text):
        clean = str(text or "").replace("**", "")
        return "灵兽" in clean and "体力不足" in clean

    def parse_required_beast_stamina(self, text, default=BEAST_ABYSS_MIN_STAMINA):
        clean = str(text or "").replace("**", "")
        for pattern in (
            r"至少需要\s*(\d+)\s*点?\s*体力",
            r"需要\s*(\d+)\s*点?\s*体力",
        ):
            match = re.search(pattern, clean)
            if match:
                return int(match.group(1))
        return default

    def parse_current_beast_stamina(self, text):
        """解析体力不足回执中的当前体力，用于修正缓存。"""
        clean = str(text or "").replace("**", "")
        for pattern in (
            r"当前(?:体力)?\s*[:：]?\s*(\d+)",
            r"现有(?:体力)?\s*[:：]?\s*(\d+)",
            r"剩余(?:体力)?\s*[:：]?\s*(\d+)",
        ):
            match = re.search(pattern, clean)
            if match:
                return int(match.group(1))
        return -1

    def record_beast_stamina_shortage(self, beast_name, text, action="abyss"):
        default_required = {
            "border patrol": BEAST_BORDER_PATROL_MIN_STAMINA,
            "steal": BEAST_STEAL_MIN_STAMINA,
        }.get(action, BEAST_ABYSS_MIN_STAMINA)
        required = self.parse_required_beast_stamina(text, default=default_required)
        current_stamina = self.parse_current_beast_stamina(text)
        inferred_stamina = current_stamina if current_stamina >= 0 else max(0, required - 1)
        self.set_cached_beast_stamina(beast_name, inferred_stamina)
        if action in ("abyss", "border patrol"):
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 1800)
        log.info(
            f"Beast {action}: {beast_name} stamina {inferred_stamina} below "
            f"{required}; trying fallback candidate."
        )
        self.save_state()
        return required

    def abyss_candidate_beasts(self, cache=None):
        candidates = self.beast_action_candidates("abyss", cache, BEAST_ABYSS_MIN_STAMINA)
        if any(
            miniapp_beast_abyss_power_in_range(
                beast.get("power"),
                match_all_when_empty=False,
            )
            for beast in candidates
        ):
            filtered = []
            for beast in candidates:
                if not miniapp_beast_abyss_power_in_range(
                    beast.get("power"),
                    match_all_when_empty=True,
                ):
                    log.info(
                        f"Abyss candidate skipped: {beast.get('full_name', '')} power "
                        f"{beast.get('power', 0)} is outside configured abyss range."
                    )
                    continue
                filtered.append(beast)
            return sorted(
                filtered,
                key=lambda b: (
                    b.get("power", 0),
                    self.beast_stamina_value(b),
                    b.get("exp", 0),
                    b.get("full_name", ""),
                ),
                reverse=True,
            )
        focus = None
        for beast in candidates:
            if self.beast_name_matches(beast.get("full_name", ""), BEAST_FOCUS_NAME):
                focus = beast
                break

        ordered = []
        if focus and self.beast_stamina_value(focus) >= BEAST_FOCUS_PROTECT_STAMINA:
            ordered.append(focus)
            log.info(
                f"Abyss candidate priority: {BEAST_FOCUS_NAME} stamina "
                f"{self.beast_stamina_value(focus)} >= {BEAST_FOCUS_PROTECT_STAMINA}."
            )

        tier_one = []
        fallback = []
        for beast in candidates:
            if focus is beast:
                continue
            tier = self.beast_tier_value(beast)
            if tier == 1:
                tier_one.append(beast)
            else:
                fallback.append(beast)

        if tier_one:
            ordered.extend(tier_one)
        else:
            ordered.extend(fallback)
        return ordered

    async def execute_abyss_with_fallback(self, defer_focus_pasture=False):
        """探渊前按防刷屏标准更新灵兽缓存；六翼>=50优先，否则按候选体力/战力补位。"""
        log.info("Abyss: checking beast roster cache before selecting candidate.")
        if not await self.update_beast_cache():
            retry_at = self.state.get("next_beast_status_check_time", "")
            if retry_at and is_future(retry_at):
                log.info(f"Abyss: beast cache unavailable; retry after roster refresh window {retry_at}.")
                self.set_next_abyss_not_before(retry_at)
                self.save_state()
            else:
                log.info("Abyss: failed to refresh beast cache; retry later.")
                self.schedule_abyss_retry(1800)
            return False

        candidates = self.abyss_candidate_beasts(self.state.get("beasts_cache", []))
        if not candidates:
            log.warning("Abyss: no 一阶 beast candidate has enough stamina/status; retry after status refresh.")
            if self.all_cached_beasts_pastured(self.state.get("beasts_cache", [])):
                retry_at = self.pasture_block_until()
                self.state["next_beast_status_check_time"] = retry_at
                self.set_next_abyss_not_before(retry_at)
                log.info(f"Abyss: all cached beasts are pastured; next status refresh delayed until {retry_at}.")
            else:
                self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 1800)
                self.schedule_abyss_retry(1800)
            self.save_state()
            return False

        last_response = ""
        for beast in candidates:
            best_name = beast.get("full_name", "")
            best_status = beast.get("status", "未知")
            if not best_name:
                continue
            self.update_best_beast_tracking(beast)
            self.save_state()
            if self.should_rest_before_abyss(best_status):
                if self.is_pastured_status(best_status):
                    ok, rest_resp = await self.recall_pastured_beast_for_action(best_name, "abyss")
                    if not ok:
                        last_response = rest_resp or last_response
                        continue
                    best_status = "休息中"
                else:
                    rest_status, rest_resp = await self.rest_beast_for_abyss(best_name)
                    if rest_status:
                        best_status = rest_status
                    else:
                        log.warning(f"Abyss: {best_name} rest failed before abyss; trying next candidate.")
                        if self.is_beast_stamina_insufficient_response(rest_resp):
                            self.record_beast_stamina_shortage(best_name, rest_resp, "abyss")
                        elif rest_resp and self.is_beast_pastured_response(rest_resp, best_name):
                            ok, recall_resp = await self.recall_pastured_beast_for_action(best_name, "abyss")
                            if ok:
                                best_status = "休息中"
                            else:
                                last_response = recall_resp or rest_resp or last_response
                                continue
                        else:
                            continue
                await asyncio.sleep(3)
                if not self.can_attempt_abyss_status(best_status):
                    log.warning(f"Abyss: {best_name} cannot enter after recall; status is {best_status}.")
                    continue

            a_resp = await self.send_abyss_with_busy_retry(best_name)
            last_response = a_resp or last_response
            if not a_resp:
                self.schedule_abyss_retry(1800)
                return False
            if self.handle_no_such_beast_response(best_name, a_resp, "灵兽探渊"):
                await asyncio.sleep(3)
                continue
            if self.is_beast_stamina_insufficient_response(a_resp):
                self.record_beast_stamina_shortage(best_name, a_resp, "abyss")
                await asyncio.sleep(3)
                continue

            reward_recorded = False
            if any(k in a_resp for k in ["获得", "收获", "战利品", "带回", "奖励"]):
                reward_recorded = self.record_daily_reward_event(
                    "主魂", f".探渊 {best_name}", a_resp, source=f"灵兽探渊[{best_name}]"
                )

            injury_cd = self.record_beast_injury_from_response(best_name, a_resp, source="abyss")
            if injury_cd >= 0:
                abyss_delay = max(21600, injury_cd)
                self.state["last_abyss_time"] = add_seconds_str(now_str(), abyss_delay - 21600)
                self.state["next_abyss_time"] = add_seconds_str(now_str(), abyss_delay)
                self.save_state()
                return True

            a_cd = self.parse_wait_time(a_resp)
            if a_cd > 0:
                self.state["last_abyss_time"] = add_seconds_str(now_str(), a_cd - 21600)
                self.state["next_abyss_time"] = add_seconds_str(now_str(), a_cd)
                self.save_state()
                return True

            if self.is_abyss_success_response(a_resp):
                if not reward_recorded:
                    self.record_daily_reward_event(
                        "主魂", f".探渊 {best_name}", a_resp, source=f"灵兽探渊[{best_name}]"
                    )
                self.state["last_abyss_time"] = now_str()
                self.state["next_abyss_time"] = add_seconds_str(now_str(), 21600)
                self.set_best_beast_status(best_name, "休息中")
                abyss_settled = any(k in a_resp for k in ["获得", "收获", "战利品", "带回", "奖励", "击败"])
                if self.beast_name_matches(best_name, BEAST_FOCUS_NAME) and abyss_settled:
                    if defer_focus_pasture:
                        self.queue_focus_pasture_after_priority_action(best_name, "abyss; waiting for steal")
                    else:
                        await self.attempt_focus_pasture_after_abyss(best_name)
                self.save_state()
                return True

            notify_unrecognized_response(self, ".灵兽探渊", a_resp, log, f"灵兽探渊[{best_name}]")
            self.schedule_abyss_retry()
            return False

        log.warning(f"Abyss: all candidates failed or lacked stamina. Last response: {last_response[:80]}")
        self.schedule_abyss_retry(1800)
        self.save_state()
        return False

    # ---- 灵兽：缓存解析 ----

    def parse_beasts_info(self, text):
        """解析.我的灵兽回复，提取每只灵兽的详细信息"""
        beasts = []
        if not text: return beasts
        clean = text.replace('（', '(').replace('）', ')').replace('**', '')
        if "灵兽归来" in clean and "灵兽伙伴们" not in clean:
            log.info("Beast cache parser ignored pasture-return settlement text.")
            return beasts
        if "灵兽伙伴们" not in clean and not re.search(r"\n-\s*[^\n]+\n\s*-\s*种类[:：]", clean):
            log.info("Beast cache parser ignored non-roster text.")
            return beasts
        blocks = re.split(r'\n-\s*', clean)
        for block in blocks:
            if not block.strip() or "灵兽伙伴们" in block: continue
            lines = block.strip().split('\n')
            header = lines[0]
            if not self.is_valid_beast_name(header):
                continue
            brackets = re.findall(r'\(([^)]+)\)', header)
            name_base = re.sub(r'\(.*?\)', '', header).replace('-', '').strip()
            if not self.is_valid_beast_name(name_base):
                continue
            status_words = ("出战中", "休息中", "放养中", "受伤", "重伤", "治疗中", "探险中", "偷菜中", "巡游中", "巡边中")
            status = "未知"
            suffix_parts = brackets
            if brackets and any(word in brackets[-1] for word in status_words):
                status = brackets[-1]; suffix_parts = brackets[:-1]
            suffix = " ".join(f"({b})" for b in suffix_parts)
            full_name = f"{name_base} {suffix}".strip()
            species_match = re.search(r'种类[:：]\s*([^\n]+)', block)
            if not species_match:
                continue
            species = species_match.group(1).strip()
            exp_match = re.search(r'经验[:：]\s*(\d+)', block)
            power_match = re.search(r'战力[:：]\s*(\d+)', block)
            stamina_match = re.search(r'体力[:：]\s*(\d+)', block)
            if not power_match or not stamina_match:
                continue
            beast = {
                'full_name': full_name, 'status': status,
                'status_cd': self.parse_wait_time(block) if any(k in status for k in ["受伤", "治疗"]) else -1,
                'species': species,
                'tier': self.beast_tier_value(species),
                'exp': int(exp_match.group(1)) if exp_match else 0,
                'power': int(power_match.group(1)) if power_match else 0,
                'stamina': int(stamina_match.group(1)) if stamina_match else -1,
            }
            if self.is_valid_beast_record(beast):
                beasts.append(beast)
        return self.sorted_beasts_by_power(beasts)

    def record_beast_roster_response(self, text, source=""):
        """从 .我的灵兽 回复同步灵兽缓存。"""
        if not text or "灵兽" not in text:
            return False
        self.record_beast_roster_response_metadata(text, source, "received")
        if self.is_pasture_return_message(text):
            self.mark_pastured_beasts_returned(text)
            self.clear_pasture_pending()
            self.state["last_pasture_return_time"] = now_str()
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 1800)
            self.wake_overdue_beast_border_patrol(f"pasture return roster sync {source or 'reply'}")
            self.record_beast_roster_response_metadata(text, source, "pasture_return")
            self.save_state()
            log.info(f"Beast roster sync skipped by pasture-return settlement ({source or 'unknown'}); cache preserved.")
            return True
        beasts = self.parse_beasts_info(text)
        if not beasts:
            self.record_beast_roster_response_metadata(text, source, "not_roster")
            return False
        self.preserve_active_beast_statuses_for_roster(beasts)
        self.state["beasts_cache"] = beasts
        self.state["beast_roster_updated_at"] = now_str()
        self.record_beast_roster_response_metadata(text, source, "parsed")
        self.sync_border_patrol_from_roster(beasts)
        self.update_best_beast_tracking()
        self.should_stop_hunt_by_tenth_beast(beasts)
        self.save_state()
        log.info(f"Beast roster synced from {source or 'reply'}: {len(beasts)} beasts.")
        return True

    def record_beast_roster_response_metadata(self, text, source="", result=""):
        """记录最近一次灵兽列表相关回执，避免只发送不留痕。"""
        excerpt = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(excerpt) > 240:
            excerpt = excerpt[:240] + "..."
        self.state["last_beast_roster_response_excerpt"] = excerpt
        self.state["beast_roster_last_source"] = source or ""
        if result:
            self.state["last_beast_roster_query_result"] = result

    def beast_roster_auto_query_today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def normalize_beast_roster_auto_query_quota(self):
        today = self.beast_roster_auto_query_today()
        changed = False
        if self.state.get("beast_roster_auto_query_date") != today:
            self.state["beast_roster_auto_query_date"] = today
            self.state["beast_roster_auto_query_count"] = 0
            changed = True
        try:
            count = int(self.state.get("beast_roster_auto_query_count", 0) or 0)
        except Exception:
            count = 0
            changed = True
        if count < 0:
            count = 0
            changed = True
        if self.state.get("beast_roster_auto_query_count") != count:
            self.state["beast_roster_auto_query_count"] = count
            changed = True
        return changed

    def next_beast_roster_auto_query_reset_time(self):
        tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return dt_to_str(tomorrow)

    def beast_roster_auto_query_remaining(self):
        self.normalize_beast_roster_auto_query_quota()
        used = int(self.state.get("beast_roster_auto_query_count", 0) or 0)
        return max(0, BEAST_ROSTER_AUTO_DAILY_LIMIT - used)

    def record_beast_roster_auto_query_sent(self):
        self.normalize_beast_roster_auto_query_quota()
        used = int(self.state.get("beast_roster_auto_query_count", 0) or 0) + 1
        self.state["beast_roster_auto_query_count"] = used
        self.state["last_beast_roster_query_time"] = now_str()
        self.state["last_beast_roster_query_result"] = "sent"
        self.state["last_beast_roster_response_excerpt"] = ""
        if used >= BEAST_ROSTER_AUTO_DAILY_LIMIT:
            self.state["next_beast_status_check_time"] = self.next_beast_roster_auto_query_reset_time()
        return used

    def defer_beast_roster_auto_query_after_limit(self):
        reset_at = self.next_beast_roster_auto_query_reset_time()
        self.state["next_beast_status_check_time"] = reset_at
        self.state["last_beast_roster_query_result"] = "daily_limit"
        self.save_state()
        return reset_at

    def schedule_beast_roster_retry_after_auto_query(self, retry_seconds=1800):
        if self.beast_roster_auto_query_remaining() <= 0:
            self.state["next_beast_status_check_time"] = self.next_beast_roster_auto_query_reset_time()
        else:
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), retry_seconds)

    def preserve_active_beast_statuses_for_roster(self, beasts):
        """本地已确认的长时任务状态优先于 .我的灵兽 的短暂/滞后状态。"""
        patrol_name = str(self.state.get("beast_border_patrol_name") or "").strip()
        patrol_next = self.state.get("next_beast_border_patrol_time", "")
        if patrol_name and patrol_next and is_future(patrol_next):
            for beast in beasts:
                if self.beast_name_matches(beast.get("full_name", ""), patrol_name):
                    beast["full_name"] = patrol_name
                    beast["status"] = "巡边中"
                    break

    def clear_border_patrol_cache_statuses(self, except_name=""):
        except_name = str(except_name or "").strip()
        for beast in self.state.get("beasts_cache", []) or []:
            name = beast.get("full_name", "")
            if "巡边" not in str(beast.get("status") or ""):
                continue
            if except_name and self.beast_name_matches(name, except_name):
                continue
            beast["status"] = "休息中"

    def sync_border_patrol_from_roster(self, beasts):
        """Use .我的灵兽 as a backstop for patrol state when manual commands changed it."""
        patrols = [b for b in (beasts or []) if "巡边" in str(b.get("status") or "")]
        if len(patrols) != 1:
            if not patrols and str(self.state.get("beast_border_patrol_name") or "").strip():
                next_patrol = self.state.get("next_beast_border_patrol_time", "")
                if not next_patrol or not is_future(next_patrol):
                    self.state["beast_border_patrol_name"] = ""
            return False

        name = patrols[0].get("full_name", "")
        if not name:
            return False
        previous = str(self.state.get("beast_border_patrol_name") or "").strip()
        if previous and not self.beast_name_matches(previous, name):
            log.info(f"Beast border patrol roster sync: active changed {previous} -> {name}.")
        self.state["beast_border_patrol_name"] = name
        if not self.state.get("beast_border_patrol_mode"):
            self.state["beast_border_patrol_mode"] = BEAST_BORDER_PATROL_DEFAULT_MODE

        next_patrol = self.state.get("next_beast_border_patrol_time", "")
        last_patrol = self.state.get("last_beast_border_patrol_time", "")
        expected = add_seconds_str(last_patrol, BEAST_BORDER_PATROL_CD_SECONDS) if last_patrol else ""
        if expected and is_future(expected):
            self.state["next_beast_border_patrol_time"] = expected
        elif not next_patrol or not is_future(next_patrol):
            self.state["next_beast_border_patrol_time"] = add_seconds_str(now_str(), 60)
            wakeup = getattr(self, "beast_wakeup", None)
            if wakeup:
                wakeup.set()
        elif seconds_until(next_patrol) <= 600:
            wakeup = getattr(self, "beast_wakeup", None)
            if wakeup:
                wakeup.set()
        return True

    async def update_beast_cache(self):
        """按自定义防刷屏标准刷新灵兽缓存；自动 .我的灵兽 每天最多 2 次。"""
        config = getattr(self, "config", {}) or {}
        if not bool(config.get("legacy_beast_roster_command_enabled", False)):
            self.state["last_beast_roster_query_result"] = "miniapp_required"
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 1800)
            self.save_state()
            log.warning(
                "Beast Cache: .我的灵兽 has moved to Mini App; deprecated command will not be sent."
            )
            return False
        if self.normalize_beast_roster_auto_query_quota():
            self.save_state()
        cache = self.state.get("beasts_cache", []) or []
        next_check = self.state.get("next_beast_status_check_time", "")
        if next_check and is_future(next_check):
            if cache:
                log.info(f"Beast Cache: auto .我的灵兽 deferred until {next_check}; using cached roster.")
                return True
            log.warning(f"Beast Cache: auto .我的灵兽 deferred until {next_check}, but cache is empty.")
            return False

        if self.beast_roster_auto_query_remaining() <= 0:
            reset_at = self.defer_beast_roster_auto_query_after_limit()
            if cache:
                log.info(
                    f"Beast Cache: auto .我的灵兽 daily cap "
                    f"({BEAST_ROSTER_AUTO_DAILY_LIMIT}) reached; using cached roster until {reset_at}."
                )
                return True
            log.warning(
                f"Beast Cache: auto .我的灵兽 daily cap "
                f"({BEAST_ROSTER_AUTO_DAILY_LIMIT}) reached and cache is empty; next refresh after {reset_at}."
            )
            return False

        used = self.reserve_beast_roster_auto_query()
        if used is None:
            return bool(cache)
        log.info(f"Refreshing Beast Cache with .我的灵兽 ({used}/{BEAST_ROSTER_AUTO_DAILY_LIMIT} today).")
        resp = await self.send_and_wait_feedback(".我的灵兽", timeout=45, max_retries=0)
        if resp and "灵兽" in resp:
            if self.is_pasture_return_message(resp):
                self.record_beast_roster_response(resp, source="auto .我的灵兽")
                self.schedule_beast_roster_retry_after_auto_query(1800)
                self.save_state()
                log.info("Beast Cache: .我的灵兽 was interrupted by pasture return; cache preserved and refresh deferred.")
                return False
            if not self.record_beast_roster_response(resp, source="auto .我的灵兽"):
                self.schedule_beast_roster_retry_after_auto_query(1800)
                self.save_state()
                log.warning("Beast Cache: response did not contain a valid beast roster; cache preserved.")
                return False
            summary = ", ".join(
                f"{b['full_name']}(tier={b.get('tier', 0)},战力{b['power']},体力{b.get('stamina', -1)})"
                for b in self.state.get("beasts_cache", [])
            )
            log.info(f"Beast Cache: {len(self.state.get('beasts_cache', []))} parsed: {summary}")
            return True
        self.state["last_beast_roster_query_result"] = "no_response" if not resp else "not_beast_response"
        if resp:
            self.record_beast_roster_response_metadata(resp, "auto .我的灵兽", "not_beast_response")
        self.schedule_beast_roster_retry_after_auto_query(1800)
        self.save_state()
        log.warning("Beast Cache: .我的灵兽 did not return a beast roster; cache preserved.")
        return False

    def beast_name_from_command(self, command):
        """从灵兽指令中提取灵兽名。"""
        parts = str(command or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            return self.state.get("best_beast_name", "")
        return parts[1].strip()

    def parse_beast_current_status_response(self, text):
        """解析“当前正在(受伤/放养中/出战中)”这类阻塞回复。"""
        clean = str(text or "").replace("**", "").replace("（", "(").replace("）", ")")
        name_match = re.search(r"灵兽【([^】]+)】", clean)
        status_match = re.search(r"当前(?:正在|并非出战状态，而是正在)\(([^)]+)\)", clean)
        if not status_match:
            status_match = re.search(r"正在\(([^)]+)\)", clean)
        return (
            name_match.group(1).strip() if name_match else "",
            status_match.group(1).strip() if status_match else "",
        )

    def record_beast_current_status_response(self, text, beast_name="", source="", retry_key="", retry_seconds=1800):
        """同步“当前正在(...)，无法...”类回执，避免同一灵兽被多个动作抢用。"""
        resp_name, resp_status = self.parse_beast_current_status_response(text)
        target_name = resp_name or beast_name
        if not target_name or not resp_status:
            return False

        self.set_best_beast_status(target_name, resp_status)
        cd = self.parse_wait_time(text)
        retry = cd if cd > 0 else retry_seconds

        if "巡边" in resp_status:
            self.state["beast_border_patrol_name"] = target_name
            if cd > 0:
                self.state["next_beast_border_patrol_time"] = add_seconds_str(now_str(), cd)
            else:
                current = self.state.get("next_beast_border_patrol_time", "")
                if not current or not is_future(current):
                    self.state["next_beast_border_patrol_time"] = add_seconds_str(now_str(), BEAST_ACTION_RETRY_SECONDS)

        if "巡游" in resp_status and cd > 0:
            self.state["next_beast_cruise_time"] = add_seconds_str(now_str(), cd)
        if "偷菜" in resp_status and cd > 0:
            self.state["next_steal_time"] = add_seconds_str(now_str(), cd)
        if "探险" in resp_status and cd > 0:
            self.state["next_abyss_time"] = add_seconds_str(now_str(), cd)

        if retry_key:
            self.schedule_beast_action_retry(retry_key, retry)
        else:
            self.save_state()
        log.info(
            f"Beast status synced from {source or 'status response'}: "
            f"{target_name} -> {resp_status}; retry in {retry}s."
        )
        return True

    def record_manual_beast_command_response(self, command, text):
        """同步手动灵兽指令的机器人回复到 state。"""
        cmd = str(command or "").strip()
        if not cmd:
            return False
        if cmd == ".我的灵兽":
            return self.record_beast_roster_response(text, source="manual .我的灵兽")
        if cmd.startswith(".灵兽巡边"):
            beast_name, mode = self.parse_beast_border_patrol_command(cmd)
            if self.handle_no_such_beast_response(beast_name, text, f"manual {cmd}"):
                return True
            return bool(self.record_beast_border_patrol_response(text, beast_name, mode))
        if cmd == ".巡边状态":
            return bool(self.record_beast_border_patrol_status_response(text))
        if cmd == ".巡边归来":
            return bool(self.record_beast_border_patrol_return_response(text))

        beast_name = self.beast_name_from_command(cmd)
        resp_name, resp_status = self.parse_beast_current_status_response(text)
        if resp_name:
            beast_name = resp_name
        if self.handle_no_such_beast_response(beast_name, text, f"manual {cmd}"):
            return True

        if cmd.startswith(".灵兽互动 "):
            return bool(self.record_beast_interaction_response(text, cmd))
        if cmd.startswith(".灵兽巡游 "):
            return bool(self.record_beast_cruise_response(text, beast_name))

        if cmd.startswith(".灵兽出战 "):
            if self.is_beast_deploy_success(text):
                self.set_best_beast_status(beast_name, "出战中")
                return True
            injury_cd = self.record_beast_injury_from_response(beast_name, text, source="manual deploy")
            if injury_cd >= 0:
                return True
            if self.is_beast_pastured_response(text, beast_name) or self.is_pastured_status(resp_status):
                self.set_best_beast_status(beast_name, "放养中")
                return True
            if resp_status:
                self.set_best_beast_status(beast_name, resp_status)
                return True
            return False

        if cmd.startswith(".灵兽休息 "):
            rest_status = self.parse_rest_response_status(text)
            if rest_status:
                self.set_best_beast_status(beast_name, rest_status)
                return True
            injury_cd = self.record_beast_injury_from_response(beast_name, text, source="manual rest")
            if injury_cd >= 0:
                return True
            if self.is_beast_pastured_response(text, beast_name) or self.is_pastured_status(resp_status):
                self.set_best_beast_status(beast_name, "放养中")
                return True
            if resp_status:
                self.set_best_beast_status(beast_name, resp_status)
                return True
            return False

        if cmd.startswith(".探渊 ") or cmd.startswith(".灵兽探渊 "):
            injury_cd = self.record_beast_injury_from_response(beast_name, text, source="abyss")
            if injury_cd >= 0:
                abyss_delay = max(21600, injury_cd)
                self.state["last_abyss_time"] = add_seconds_str(now_str(), abyss_delay - 21600)
                self.state["next_abyss_time"] = add_seconds_str(now_str(), abyss_delay)
                self.save_state()
                return True
            if self.is_beast_stamina_insufficient_response(text):
                self.record_beast_stamina_shortage(beast_name, text, "abyss")
                return True
            if self.is_abyss_busy_response(text):
                if resp_status:
                    self.set_best_beast_status(beast_name, resp_status)
                self.schedule_abyss_retry(1800)
                return True
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.state["last_abyss_time"] = add_seconds_str(now_str(), cd - 21600)
                self.state["next_abyss_time"] = add_seconds_str(now_str(), cd)
                self.save_state()
                return True
            if any(k in str(text or "") for k in ["送入万兽渊", "正在与"]):
                self.state["last_abyss_time"] = now_str()
                self.state["next_abyss_time"] = add_seconds_str(now_str(), 21600)
                self.set_best_beast_status(beast_name, "探险中")
                self.save_state()
                return True
            if self.is_abyss_success_response(text):
                self.state["last_abyss_time"] = now_str()
                self.state["next_abyss_time"] = add_seconds_str(now_str(), 21600)
                self.set_best_beast_status(beast_name, "休息中")
                abyss_settled = any(k in str(text or "") for k in ["获得", "收获", "战利品", "带回", "奖励", "击败"])
                if self.beast_name_matches(beast_name, BEAST_FOCUS_NAME) and abyss_settled:
                    self.queue_focus_pasture_after_priority_action(beast_name, "abyss settlement")
                    beast_wakeup = getattr(self, "beast_wakeup", None)
                    if beast_wakeup is not None:
                        beast_wakeup.set()
                self.save_state()
                return True
            return False

        if cmd == ".灵兽偷菜":
            cd = self.parse_wait_time(text)
            if cd > 0:
                self.state["last_steal_time"] = add_seconds_str(now_str(), cd - 14400)
                self.state["next_steal_time"] = add_seconds_str(now_str(), cd)
                self.save_state()
                return True
            if self.is_no_beast_deployed_for_steal_response(text):
                self.state["next_steal_time"] = add_seconds_str(now_str(), 600)
                self.save_state()
                return True
            if self.is_steal_accepted_response(text):
                self.state["last_steal_time"] = now_str()
                self.state["next_steal_time"] = add_seconds_str(now_str(), 14400)
                best_name = self.state.get("best_beast_name", "")
                if best_name:
                    self.set_best_beast_status(
                        best_name,
                        "偷菜中" if self.is_steal_pending_response(text) else "出战中",
                    )
                if self.beast_name_matches(best_name, BEAST_FOCUS_NAME):
                    if self.is_steal_pending_response(text):
                        self.schedule_focus_pasture_after_abyss(
                            best_name,
                            retry_seconds=BEAST_ACTION_RETRY_SECONDS,
                        )
                    elif self.is_steal_settled_response(text):
                        self.queue_focus_pasture_after_priority_action(best_name, "steal settlement")
                        beast_wakeup = getattr(self, "beast_wakeup", None)
                        if beast_wakeup is not None:
                            beast_wakeup.set()
                self.save_state()
                return True
        return False

    # ---- 关键词提醒 ----

    def should_send_keyword_alert(self, msg, text):
        """判断是否应发送关键词告警"""
        if not text: return False
        if is_boss_monitor_alert_text(text): return False
        lower_text = text.lower()
        return any(k in lower_text for k in self.keywords) and (mentions_self(self, msg, text) or any(f"@{u}" in lower_text or f"【{u}】" in lower_text for u in self.notify_users))

    def text_targets_self(self, msg, text):
        """判断消息是否针对本账号"""
        if mentions_self(self, msg, text): return True
        lower_text = (text or "").lower()
        candidates = []
        if self.my_info: candidates.extend([getattr(self.my_info, "username", "") or "", getattr(self.my_info, "first_name", "") or ""])
        candidates.extend(self.notify_users)
        for values in (getattr(self, "identity_usernames", {}) or {}).values():
            if isinstance(values, str):
                candidates.append(values)
            else:
                candidates.extend(values or [])
        for value in candidates:
            name = str(value or "").lower().lstrip("@").strip()
            if not name: continue
            if f"@{name}" in lower_text or f"【{name}】" in lower_text: return True
            if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lower_text): return True
        return False

    def is_anti_bot_challenge(self, text):
        return bool(text and any(k in text for k in ["挂机嫌疑", "天道审判", "自证", "天道裁决", "挂机傀儡", "死株", "死牢"]))

    def anti_bot_challenge_targets_self(self, msg, text, sender):
        return self.is_anti_bot_challenge(text) and is_game_bot_sender(self, sender) and self.text_targets_self(msg, text)

    async def send_keyword_alert(self, msg, text, title="万灵宗提醒"):
        """发送关键词告警给用户"""
        msg_id = getattr(msg, "id", None)
        if msg_id in self.notified_alert_ids: return
        if msg_id is not None:
            self.notified_alert_ids.add(msg_id)
            if len(self.notified_alert_ids) > 300: self.notified_alert_ids = set(list(self.notified_alert_ids)[-150:])
        alert_text = f"【{title}】\n{text}"
        target = self.config.get("notify_target", "Waaiging")
        try:
            final_target = target
            if isinstance(target, str) and not target.startswith("@") and not target.lstrip("-").isdigit(): final_target = f"@{target}"
            sent_via_bot = False
            bot_token = self.config.get("notify_bot_token")
            if bot_token:
                import urllib.request
                data = json.dumps({"chat_id": final_target, "text": alert_text}).encode("utf-8")
                req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=data, headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=5): sent_via_bot = True
                except Exception as e: log.error(f"Alert bot send failed, falling back to client: {e}")
            if not sent_via_bot: await self.client.send_message(final_target, alert_text)
            log.info(f"Alert sent to {final_target}.")
        except Exception as e: log.error(f"Alert send failed: {e}")

    def is_beast_bag_full_response(self, text):
        return bool(text and any(k in text for k in ["灵兽袋已满", "最多只能容纳", "请使用 `.放生", "请使用 .放生", "腾出空间"]))

    def is_no_such_beast_response(self, text):
        return bool(text and any(k in text for k in ["没有名为", "没有这只灵兽", "未找到该灵兽", "不存在该灵兽"]))

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
            return (is_deep_meditation_ongoing_response(text) or is_deep_meditation_settlement_response(text) or is_not_deep_meditation_response(text))
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

    # ---- 消息处理 ----

    async def handle_game_response(self, event):
        """游戏消息处理器：检测回复、更新状态、触发自动回复"""
        try:
            msg = event.message; text = (msg.text or ""); msg_text_lower = text.lower()
            sender = await event.get_sender()
            # 所有主魂/化身 @ 提及先记日志，不能被反馈匹配或其他提前返回吞掉。
            log_mention_if_needed(
                self, msg, text=text, sender=sender, mentions_only=True
            )
            record_message_event(self, msg, text=text, sender=sender, event_kind="new", direction="raw", logger=log)
            record_star_gazing_event("xiaohao", msg, text, sender=sender, logger=log)
            if is_game_bot_sender(self, sender):
                record_game_bot_activity(self, sender, log, msg=msg, text=text)
                self.record_star_gazing_final_report_if_needed(msg, text, source="new message")
                self.record_star_shift_attempt_if_needed(msg, text, source="new message")
            # 身外化身：被动身份自愈更新
            if is_game_bot_sender(self, sender): 
                self.update_identity_passively(msg)
                await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                self.maybe_record_avatar_passive_states(msg)
                await self.maybe_record_fishing_rod_message(msg, text, sender)
            if await handle_anti_bot_challenge(self, msg, text, sender, log, title="万灵宗自证告警"): return
            if is_game_bot_sender(self, sender) and self.should_send_keyword_alert(msg, text): await self.send_keyword_alert(msg, text, title="万灵宗关键词提醒")
            self.maybe_record_field_training_passive(msg, text)
            self.maybe_handle_sect_war_message(msg, text, sender)
            if is_game_bot_sender(self, sender): self.maybe_record_manual_pasture_dispatch(msg, text)
            self.remember_manual_pasture_command_if_needed(msg, text)
            
            # --- 阵法助阵拦截 ---
            if is_game_bot_sender(self, sender) and self.is_target_formation_invite(text):
                asyncio.create_task(self.handle_global_formation_invite(msg))
                    
            # --- 观星显化拦截（轮换派发：每次只派一个化身） ---
            if is_game_bot_sender(self, sender) and ("【Good -" in text or "【星盘显化】" in text):
                asyncio.create_task(self.avatar_handle_star_gazing_opportunity(None, msg, text, sender))

            # ---- 控制指令：止/启（仅管理员可触发） ----
            # 必须在 log_manual_outgoing_if_needed 之前，否则手动发的"止"会被拦截
            # chat_id 比较需兼容 Telethon 的 -100 前缀（supergroup）
            _chat_id_match = _chat_matches_actor_target(self, msg)
            if not is_game_bot_sender(self, sender) and _chat_id_match:
                if await handle_clear_history_command(self, msg, text, sender, log):
                    return
                if await handle_pause_control_command(self, msg, text, sender, log, label="万灵宗脚本"):
                    return
                if await self.maybe_handle_fishing_control_message(msg, text, sender):
                    return

            if log_manual_outgoing_if_needed(self, msg, text=text): return
            if await maybe_handle_han_soul_choice(self, msg, text, sender, log): return
            if is_auto_reply_followup(self, msg, sender=sender): return
            if await maybe_auto_reply_exchange(self, event, text=text, sender=sender): return

            is_matched = False
            # 1. 回复匹配（最优先）
            is_matched = match_pending_feedback_by_reply(
                self, msg, text, self.is_loose_meditation_feedback_candidate, log,
                label="[REPLY-FEEDBACK]", sender=sender
            )
            if not is_matched and await self.handle_pasture_return_event(event, text=text, sender=sender): return
            # 2. 用户名/昵称匹配
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
                    lambda command, body: (
                        self.is_loose_meditation_feedback_candidate(command, body)
                        or (command == ".一键放养" and self.is_no_resting_pasture_response(body))
                    ),
                    log,
                    id_window=30,
                    label="[MENTION-FEEDBACK]",
                    sender=sender,
                )
            # 3. 宽松匹配（针对特定指令如闭关、一键放养）
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
                        lambda command, body: (
                            self.is_loose_meditation_feedback_candidate(command, body)
                            or (command == ".一键放养" and self.is_no_resting_pasture_response(body))
                        ),
                        log,
                        id_window=30,
                        label="[LOOSE-FEEDBACK]",
                        sender=sender,
                    )
            if not is_matched:
                log_mention_if_needed(self, msg, text=text, sender=sender)
                if "黄枫谷" in text and "药园" in text: return
                if await maybe_auto_reply_exchange(self, event, text=text): return
        except Exception as e: log.error(f"Handler Error: {e}")

    # ---- 星宫阵法助阵：小号只助阵副号三分身的启阵邀请 ----

    def formation_invite_actor_username(self, text):
        return CommonCommandMixin.formation_invite_actor_username(self, text)

    def avatar_username_for_identity(self, avatar):
        return CommonCommandMixin.avatar_username_for_identity(self, avatar)

    def formation_result_includes_avatar(self, text, avatar):
        return CommonCommandMixin.formation_result_includes_avatar(self, text, avatar)

    def is_target_formation_invite(self, text):
        if not text:
            return False
        if "周天星斗大阵-成" in text or "大阵已成" in text:
            return False
        if not (
            "周天星斗大阵-启" in text
            and "正在布设大阵" in text
            and ("尚需" in text or "助阵" in text)
        ):
            return False
        return self.formation_invite_actor_username(text) in FORMATION_TARGET_INITIATORS

    def message_age_seconds(self, msg):
        return CommonCommandMixin.message_age_seconds(self, msg)

    def record_avatar_formation_success(self, avatar, formation_time=None):
        formation_time = formation_time or now_str()
        force_delay = 5 * 3600 + 55 * 60
        self.set_avatar_state(avatar, "last_formation_time", formation_time)
        self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(formation_time, 12 * 3600))
        self.set_avatar_state(avatar, "formation_active_until", add_seconds_str(formation_time, 6 * 3600))
        self.set_avatar_state(avatar, "next_formation_retry_time", "")
        self.set_avatar_state(avatar, "next_force_exit_time", add_seconds_str(formation_time, force_delay))
        if seconds_until(add_seconds_str(formation_time, force_delay)) > 0:
            asyncio.create_task(self.delayed_avatar_force_exit(avatar, seconds_until(add_seconds_str(formation_time, force_delay))))

    async def prepare_avatar_for_formation_assist(self, avatar):
        a_state = self.get_avatar_state(avatar)
        if a_state.get("in_deep_meditation"):
            log.info(f"Avatar {avatar}: trying direct formation assist while in deep meditation; no force exit before success.")
        return True

    FORMATION_PAIRS = {
        "缘生子": "素心子",
        "素心子": "缘生子",
    }

    async def mutual_formation_assist(self, initiator, msg_id):
        """
        当 initiator 启阵后，让其配对化身去助阵。
        缘生子启阵 → 素心子助阵；素心子启阵 → 缘生子助阵。
        """
        try:
            partner = self.FORMATION_PAIRS.get(initiator)
            if not partner:
                return

            # 等待一小段时间，确保启阵消息已发出
            await asyncio.sleep(5)

            a_state = self.get_avatar_state(partner)
            # 检查助阵冷却
            next_form = a_state.get("next_formation_time", "")
            if next_form and is_future(next_form):
                log.info(f"[互助阵] {partner} formation CD until {next_form}, skipping assist for {initiator}")
                return

            log.info(f"[互助阵] {initiator} 启阵 → {partner} 助阵 (msg_id={msg_id})")
            resp = await self.send_and_wait_feedback_identity(
                partner,
                ".助阵",
                reply_to=msg_id,
                timeout=30,
                max_retries=0,
                suppress_no_response_alert=True,
            )
            resp_str = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""

            if resp_str:
                if any(k in resp_str for k in ["助阵成功", "大阵已成", "成功助阵", "阵成", "加入大阵", "已经参与"]):
                    log.info(f"[互助阵] {partner} successfully assisted {initiator}!")
                    cd = self.parse_wait_time(resp_str)
                    cd_seconds = cd if cd > 0 else 7200
                    self.set_avatar_state(partner, "next_formation_time", add_seconds_str(now_str(), cd_seconds))
                    self.save_state()
                elif any(k in resp_str for k in ["命令保护提醒", "已暂停该命令", "暂停该命令"]):
                    cd = self.parse_wait_time(resp_str)
                    cd_seconds = cd if cd > 0 else 3600
                    self.state["next_formation_ban_time"] = add_seconds_str(now_str(), cd_seconds)
                    self.save_state()
                    log.warning(f"[互助阵] Command banned until {self.state['next_formation_ban_time']}")
                else:
                    cd = self.parse_wait_time(resp_str)
                    if cd > 0 or any(k in resp_str for k in ["冷却", "尚未结束"]):
                        cd_seconds = cd if cd > 0 else 1800
                        self.set_avatar_state(partner, "next_formation_time", add_seconds_str(now_str(), cd_seconds))
                        self.save_state()
                    log.info(f"[互助阵] {partner} assist response: {resp_str[:100]}")
        except Exception as e:
            log.error(f"[互助阵] {initiator} error: {e}")

    async def handle_global_formation_invite(self, formation_msg):
        """处理副号三分身的阵法邀请，只让一个专用化身助阵。"""
        if not formation_msg:
            return
        text = formation_msg.text or ""
        if not self.is_target_formation_invite(text):
            return
        msg_id = formation_msg.id
        age = self.message_age_seconds(formation_msg)
        if age > 60:
            log.info(f"Target formation invite ignored: stale ({int(age)}s), msg={msg_id}.")
            return
        if self.formation_assist_in_progress:
            return
        # 前置校验：是否在全局助阵封禁期间
        form_ban = self.state.get("next_formation_ban_time", "")
        if form_ban and is_future(form_ban):
            log.warning(f"🚫 Global formation assist command is banned until {form_ban}. Ignoring invite.")
            return

        now = time.monotonic()
        if getattr(self, "last_global_assist_time", 0) and now - self.last_global_assist_time < 60:
            return
        self.last_global_assist_time = now
        initiator = self.formation_invite_actor_username(text)
        log.info(
            f"Target formation invite detected from "
            f"{FORMATION_TARGET_INITIATORS.get(initiator, initiator)} (@{initiator}), msg={msg_id}."
        )

        self.formation_assist_in_progress = True
        try:
            for avatar in FORMATION_ASSIST_AVATARS:
                a_state = self.get_avatar_state(avatar)

                next_form = a_state.get("next_formation_time", "")
                if next_form and is_future(next_form):
                    log.info(f"Avatar {avatar} skipped: formation CD active until {next_form}")
                    continue

                if self.message_age_seconds(formation_msg) > 55:
                    log.info(f"Avatar {avatar} skipped: invite nearly expired.")
                    break
                if not await self.prepare_avatar_for_formation_assist(avatar):
                    continue
                if self.message_age_seconds(formation_msg) > 60:
                    log.info(f"Avatar {avatar} skipped: invite expired after preparation.")
                    break

                log.info(f"Avatar {avatar}: sending .助阵 to invite message {msg_id}")
                resp = await self.send_and_wait_feedback_identity(
                    avatar,
                    ".助阵",
                    reply_to=msg_id,
                    timeout=30,
                    max_retries=0,
                    suppress_no_response_alert=True,
                )
                resp_str = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""

                if resp_str and any(k in resp_str for k in ["命令保护提醒", "已暂停该命令", "暂停该命令"]):
                    cd = self.parse_wait_time(resp_str)
                    cd_seconds = cd if cd > 0 else 3600
                    ban_expire = add_seconds_str(now_str(), cd_seconds)
                    self.state["next_formation_ban_time"] = ban_expire
                    self.save_state()
                    log.critical(f"⚠️ Global Formation Assist Command Banned! Suspended until {ban_expire}. Response: {resp_str}")
                    return

                if resp_str and any(k in resp_str for k in ["助阵成功", "大阵已成", "成功助阵", "阵成", "加入大阵", "已经参与", "已在阵中"]):
                    log.info(f"Avatar {avatar} successfully assisted formation!")
                    self.record_avatar_formation_success(avatar)
                    return

                cd = self.parse_wait_time(resp_str)
                if cd > 0 or any(k in resp_str for k in ["冷却", "尚未结束", "请在", "未到", "参与过布阵", "心神消耗"]):
                    cd_seconds = cd if cd > 0 else 1800
                    self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd_seconds))
                    self.save_state()
                    await asyncio.sleep(2)
                    continue
                elif resp_str:
                    log.info(f"Avatar {avatar} assist failed/response: {resp_str[:120]}")
                    if any(k in resp_str for k in ["没有找到", "过期", "无法助阵", "不能助阵", "助阵失败"]):
                        await asyncio.sleep(2)
                        continue
                for _ in range(15):
                    await asyncio.sleep(1)
                    try:
                        updated_msg = await self.client.get_messages(self.target_chat_id, ids=msg_id)
                    except Exception as e:
                        log.info(f"Avatar {avatar} assist poll failed: {e}")
                        break
                    updated_text = (updated_msg.text or "") if updated_msg else ""
                    if "周天星斗大阵-成" in updated_text or "大阵已成" in updated_text:
                        if self.formation_result_includes_avatar(updated_text, avatar):
                            log.info(f"Avatar {avatar} formation assist confirmed by edited invite.")
                            self.record_avatar_formation_success(avatar)
                            return
                        log.info(f"Avatar {avatar} edited formation succeeded without this avatar; not recording CD.")
                        return
                await asyncio.sleep(2)
        finally:
            self.formation_assist_in_progress = False

    # ---- 观星与改换星移 (化身专用) ----

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

    def star_gazing_sent_on_date(self, date_str=None):
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        if self.state.get("last_gazing_date") == date_str:
            return True
        for avatar in STAR_GAZING_ROTATING_AVATARS:
            if self.get_avatar_state(avatar).get("last_gazing_date") == date_str:
                return True
        return False

    def star_shift_done_today(self, today=None):
        today = today or datetime.now().strftime("%Y-%m-%d")
        if self.state.get("last_star_shift_date") == today:
            return True
        for avatar in STAR_GAZING_ROTATING_AVATARS:
            if self.get_avatar_state(avatar).get("last_star_shift_date") == today:
                return True
        return False

    def daily_star_gazing_fallback_dt(self, now=None):
        return self.common_daily_star_gazing_fallback_dt(
            now or datetime.now(),
            hour=STAR_GAZING_DAILY_FALLBACK_HOUR,
            minute=STAR_GAZING_DAILY_FALLBACK_MINUTE,
        )

    def has_pending_star_gazing_action(self):
        pending_shift = self.state.get("pending_star_shift_target_time", "")
        if pending_shift and is_future(pending_shift):
            return True
        for avatar in STAR_GAZING_ROTATING_AVATARS:
            pending = self.get_avatar_state(avatar).get("pending_star_gazing_target_time", "")
            if pending and is_future(pending):
                return True
        return False

    def pending_daily_star_gazing_fallback_dt(self, now=None):
        return self.common_pending_daily_star_gazing_fallback_dt(now or datetime.now())

    def record_star_gazing_final_report_if_needed(self, msg, text, source="new message"):
        if not self.is_star_gazing_final_report(text):
            return False
        manifest_dt = self.current_star_report_manifest_dt()
        manifest_key = dt_to_str(manifest_dt)
        changed = self.state.get("last_star_gazing_report_manifest_time", "") != manifest_key
        self.state["last_star_gazing_report_manifest_time"] = manifest_key
        self.state["last_star_gazing_report_time"] = now_str()

        cancelled = False
        if self.state.get("pending_star_gazing_manifest_time", "") == manifest_key:
            claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
            if claimed_avatar:
                self.clear_avatar_star_gazing_pending(claimed_avatar)
            self.clear_star_gazing_round_claim()
            cancelled = True
        self.save_state()
        if changed or cancelled:
            msg_id = getattr(msg, "id", "")
            log.info(
                f"Avatar Star gazing final report seen for {manifest_key} ({source}, msg {msg_id}); "
                f"{'cancelled pending action' if cancelled else 'marked round settled'}."
            )
        return True

    def record_star_shift_attempt_if_needed(self, msg, text, source="new message"):
        return self.common_record_star_shift_attempt_message(
            msg,
            text,
            STAR_SHIFT_TARGET,
            source=source,
            logger=log,
        )

    def is_star_gazing_forbidden_response(self, text):
        return self.common_is_star_gazing_forbidden_response(text, STAR_GAZING_FORBIDDEN_KEYWORDS)

    def is_star_gazing_valid_result(self, text):
        return self.common_is_star_gazing_valid_result(text, STAR_GAZING_VALID_RESULT_KEYWORDS)

    def get_avatar_username(self, avatar):
        return self.common_get_avatar_username(avatar)

    def next_star_manifest_dt(self, now=None):
        return self.common_next_star_manifest_dt(
            now or datetime.now(),
            interval_hours=STAR_GAZING_INTERVAL_HOURS,
        )

    def star_gazing_schedule_plan(self, now, manifest_dt):
        return self.common_star_gazing_schedule_plan(
            now,
            manifest_dt,
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

    def clear_star_gazing_round_claim(self):
        """清除账号级观星轮次占用。"""
        self.common_clear_star_gazing_round_claim()

    def star_gazing_claim_matches(self, avatar, manifest_dt):
        """确认当前任务仍是本账号在该显化轮次被指派的唯一身份。"""
        return self.common_star_gazing_claim_matches(avatar, manifest_dt, default_identity="")

    def clear_avatar_star_gazing_pending(self, avatar):
        if not avatar:
            return
        self.set_avatar_state(avatar, "pending_star_gazing_date", "")
        self.set_avatar_state(avatar, "pending_star_gazing_target_time", "")
        self.set_avatar_state(avatar, "next_star_gazing_time", "")

    def claimed_star_gazing_pending_due(self, avatar, now=None):
        """Return true when a claimed .观星 send is due and may surface as a passive result."""
        pending = self.get_avatar_state(avatar).get("pending_star_gazing_target_time", "")
        return self.common_claimed_star_gazing_pending_due(avatar, pending, now)

    def star_gazing_observer_identity(self, text):
        return self.common_star_gazing_observer_identity(text)

    def claimed_star_gazing_reply_msg_id(self, avatar, msg, text):
        msg_id = getattr(msg, "id", 0)
        if not msg_id:
            return 0
        if self.common_star_gazing_response_matches_identity(msg, text, avatar, logger=log):
            return msg_id

        log.info(
            f"Avatar {avatar} Star gazing: ignoring passive result msg {msg_id}; "
            "not tied to our pending .观星 command."
        )
        return 0

    def maybe_record_passive_claimed_star_gazing_result(self, avatar, manifest_dt, gazing_date, msg, text=""):
        """Treat a passive 星盘显化 message as the result for a due claimed .观星 command."""
        state = self.get_avatar_state(avatar)
        if not avatar or state.get("last_gazing_date") == gazing_date:
            return False
        if not self.claimed_star_gazing_pending_due(avatar):
            return False

        reply_msg_id = self.claimed_star_gazing_reply_msg_id(avatar, msg, text)
        if not reply_msg_id:
            return False

        self.set_avatar_state(avatar, "last_gazing_date", gazing_date)
        self.set_avatar_state(avatar, "last_gazing_time", now_str())
        self.common_mark_star_gazing_round_assigned(
            manifest_dt,
            avatar,
            source="passive .观星 result",
            logger=log,
        )
        self.clear_avatar_star_gazing_pending(avatar)
        self.state["pending_star_gazing_manifest_time"] = ""
        self.state["pending_star_gazing_fate_type"] = ""
        self.save_state()

        log.info(
            f"Avatar {avatar} Star gazing: passive .观星 result observed for "
            f"{dt_to_str(manifest_dt)}; marked {gazing_date}."
        )
        if (
            manifest_dt
            and self.star_gazing_good_opportunity(text)
            and self.get_avatar_state(avatar).get("last_star_shift_date") != gazing_date
        ):
            asyncio.create_task(self.avatar_schedule_star_shift(avatar, reply_msg_id, manifest_dt, gazing_date))
        return True

    def cleanup_stale_star_gazing_pending(self, active_avatar="", manifest_text=""):
        changed = False
        manifest_dt = str_to_dt(manifest_text) if manifest_text else None
        latest_send_dt = manifest_dt - timedelta(seconds=60) if manifest_dt else None
        has_round_claim = bool(
            self.state.get("pending_star_gazing_manifest_time", "")
            or self.state.get("star_gazing_claimed_manifest_time", "")
        )
        for candidate in STAR_GAZING_ROTATING_AVATARS:
            state = self.get_avatar_state(candidate)
            pending = state.get("pending_star_gazing_target_time", "")
            if not pending:
                continue
            pending_date = state.get("pending_star_gazing_date", "")
            pending_dt = str_to_dt(pending)
            should_clear = (
                not pending_dt
                or not is_future(pending)
                or (pending_date and state.get("last_gazing_date") == pending_date)
                or (not has_round_claim)
                or (active_avatar and candidate != active_avatar)
                or (latest_send_dt and pending_dt > latest_send_dt)
            )
            if should_clear:
                log.info(
                    f"Avatar {candidate} Star gazing: clearing stale pending .观星 "
                    f"send={pending}, manifest={manifest_text or 'none'}."
                )
                self.clear_avatar_star_gazing_pending(candidate)
                changed = True
        return changed

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

    async def retry_star_gazing_after_identity_mismatch(self, avatar, today, manifest_dt, immediate_shift):
        """观星返回主魂/错误身份提示时，强制重新切换同一化身后再试一次。"""
        if not manifest_dt:
            return
        latest_send_dt = manifest_dt - timedelta(seconds=60)
        if datetime.now() > latest_send_dt:
            return

        send_dt = min(datetime.now() + timedelta(seconds=3), latest_send_dt)
        manifest_key = dt_to_str(manifest_dt)
        self.state["star_gazing_claimed_manifest_time"] = manifest_key
        self.state["star_gazing_claimed_avatar"] = avatar
        self.state["pending_star_gazing_manifest_time"] = manifest_key
        self.state["pending_star_gazing_fate_type"] = self.state.get("pending_star_gazing_fate_type") or "Good - identity retry"
        self.set_avatar_state(avatar, "pending_star_gazing_date", today)
        self.set_avatar_state(avatar, "pending_star_gazing_target_time", dt_to_str(send_dt))
        self.set_avatar_state(avatar, "next_star_gazing_time", dt_to_str(send_dt))
        self.save_state()
        log.info(
            f"Avatar {avatar} Star gazing: retrying after forced identity mismatch "
            f"at {dt_to_str(send_dt)}."
        )
        asyncio.create_task(
            self.avatar_schedule_star_gazing_simple(
                avatar,
                send_dt,
                immediate_shift=immediate_shift,
                manifest_dt=manifest_dt,
                identity_retry=True,
                gazing_date=today,
            )
        )

    @safe_bg_task
    async def avatar_schedule_star_shift(self, avatar, reply_msg_id, target_dt, gazing_date=None):
        today = gazing_date or target_dt.strftime("%Y-%m-%d")
        fate_type = self.state.get("pending_star_gazing_fate_type", "")
        shift_dt = star_gazing_shift_dt(target_dt, fate_type=fate_type, logger=log)
        if self.get_avatar_state(avatar).get("last_star_shift_date") == today: return
        if self.star_gazing_final_report_seen(target_dt):
            log.info(
                f"Avatar {avatar} Star gazing: final report already seen for {dt_to_str(target_dt)}; "
                "skipping .改换星移."
            )
            return
        if datetime.now() > shift_dt + timedelta(seconds=STAR_GAZING_SHIFT_GRACE_SECONDS): return
        
        wait_sec = (shift_dt - datetime.now()).total_seconds()
        if wait_sec > 0: await asyncio.sleep(scheduler_sleep_seconds(wait_sec))
        
        if self.get_avatar_state(avatar).get("last_star_shift_date") == today: return
        if not self.star_gazing_claim_matches(avatar, target_dt):
            log.info(
                f"Avatar {avatar} Star gazing: pending shift for {dt_to_str(target_dt)} "
                "no longer owns the round; skipping .改换星移."
            )
            return
        if self.star_gazing_final_report_seen(target_dt):
            log.info(
                f"Avatar {avatar} Star gazing: final report arrived for {dt_to_str(target_dt)}; "
                "skipping .改换星移."
            )
            return

        self.active_atomic_task = asyncio.current_task()
        log.info(f"🔒 [ATOMIC LOCK] Acquired by AvatarStarShift-{avatar}")
        try:
            uname = self.get_avatar_username(avatar)
            if not uname:
                log.error(f"Avatar {avatar}: username mapping not found; skipping star shift.")
                return

            command = f".改换星移 @{STAR_SHIFT_TARGET}"
            if not self.common_star_gazing_reply_target_matches_identity(reply_msg_id, avatar, logger=log):
                return
            if not self.common_mark_star_shift_attempt(
                avatar,
                today,
                source="avatar scheduled dispatch",
                logger=log,
            ):
                return
            self.save_state()
            log.info(f"Avatar {avatar} Star gazing: sending {command} as reply to .观星 result {reply_msg_id}.")
            sent_msg = await self.send_and_wait_feedback_identity(
                avatar,
                command,
                reply_to=reply_msg_id,
                suppress_no_response_alert=True,
            )
            if sent_msg:
                self.set_avatar_state(avatar, "last_star_shift_date", today)
                self.set_avatar_state(avatar, "last_star_shift_time", now_str())
        finally:
            if self.active_atomic_task == asyncio.current_task():
                self.active_atomic_task = None
                log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarShift-{avatar}")

    async def avatar_schedule_star_gazing_simple(self, avatar, send_dt, immediate_shift=False, manifest_dt=None, identity_retry=False, gazing_date=None):
        now = datetime.now()
        wait_sec = (send_dt - now).total_seconds()
        if wait_sec > 0:
            log.info(
                f"Avatar {avatar} Star gazing: waiting {int(wait_sec)}s to send .观星 at {dt_to_str(send_dt)}."
            )
            await self.sleep_then_prepare_time_critical_identity(
                send_dt,
                avatar,
                command=".观星",
                lead_seconds=20,
            )

        today = gazing_date or datetime.now().strftime("%Y-%m-%d")
        if self.get_avatar_state(avatar).get("last_gazing_date") == today: return
        if manifest_dt and not self.star_gazing_claim_matches(avatar, manifest_dt):
            claimed = self.state.get("star_gazing_claimed_avatar", "")
            claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
            log.info(
                f"Avatar {avatar} Star gazing: stale task skipped for {dt_to_str(manifest_dt)}; "
                f"claimed by {claimed or 'none'} ({claimed_manifest or 'none'})."
            )
            return
        if datetime.now() < send_dt - timedelta(seconds=1): return

        self.active_atomic_task = asyncio.current_task()
        log.info(f"🔒 [ATOMIC LOCK] Acquired by AvatarStarGazing-{avatar}")
        try:
            log.info(f"Avatar {avatar} Star gazing: sending .观星 at {dt_to_str(send_dt)}.")
            resp_msg = await self.send_and_wait_feedback_identity(
                avatar,
                ".观星",
                timeout=20,
                max_retries=0,
                return_response_msg=True,
                delete_after=False,
                force_identity_check=True,
                suppress_no_response_alert=True,
            )

            if not resp_msg:
                log.info(
                    f"Avatar {avatar} Star gazing: .观星 no direct response; "
                    "keeping today's chance available."
                )
                return

            resp_text = (resp_msg.text or "")
            if not self.common_star_gazing_response_matches_identity(resp_msg, resp_text, avatar, logger=log):
                log.info(
                    f"Avatar {avatar} Star gazing: .观星 response msg {getattr(resp_msg, 'id', None)} "
                    "does not belong to this identity; keeping today's chance available."
                )
                return

            if self.is_star_gazing_forbidden_response(resp_text):
                log.info(
                    f"Avatar {avatar} Star gazing: bot says current identity is not a Star Palace disciple; "
                    "treating it as identity desync and not marking today's .观星 as used."
                )
                self.update_avatar_states(avatar, {
                    "pending_star_gazing_date": "",
                    "pending_star_gazing_target_time": "",
                    "next_star_gazing_time": "",
                })
                self.current_identity = ""
                self._main_confirmed = False
                if self.star_gazing_claim_matches(avatar, manifest_dt):
                    self.clear_star_gazing_round_claim()
                    self.save_state()
                if not identity_retry:
                    await self.retry_star_gazing_after_identity_mismatch(
                        avatar,
                        today,
                        manifest_dt,
                        immediate_shift,
                    )
                return

            if not self.is_star_gazing_valid_result(resp_text):
                log.info(
                    f"Avatar {avatar} Star gazing: .观星 response is not a usable star result; "
                    "not marking today's chance as used."
                )
                return

            self.set_avatar_state(avatar, "last_gazing_date", today)
            self.set_avatar_state(avatar, "last_gazing_time", now_str())
            self.common_mark_star_gazing_round_assigned(
                manifest_dt,
                avatar,
                source=".观星 response",
                logger=log,
            )

            if self.star_gazing_good_opportunity(resp_text):
                if immediate_shift:
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
                        log.info(
                            f"Avatar {avatar} Star gazing: GOOD result during ACTIVE window, "
                            f"but too early for shift. Waiting {wait_sec:.1f}s until {dt_to_str(shift_dt)}."
                        )
                        await asyncio.sleep(wait_sec)
                    elif now2 > shift_dt + timedelta(seconds=STAR_GAZING_SHIFT_GRACE_SECONDS):
                        log.info(
                            f"Avatar {avatar} Star gazing: skipped ACTIVE window shift; "
                            f"configured send window ended at {dt_to_str(shift_dt)}."
                        )
                        return
                    if not self.star_gazing_claim_matches(avatar, current_manifest_dt):
                        log.info(
                            f"Avatar {avatar} Star gazing: round claim cleared for "
                            f"{dt_to_str(current_manifest_dt)}; skipping .改换星移."
                        )
                        return
                    if self.star_gazing_final_report_seen(current_manifest_dt):
                        log.info(
                            f"Avatar {avatar} Star gazing: final report already seen for "
                            f"{dt_to_str(current_manifest_dt)}; skipping .改换星移."
                        )
                        return

                    uname = self.get_avatar_username(avatar)
                    if not uname:
                        log.error(f"Avatar {avatar}: username mapping not found in immediate mode; skipping shift.")
                        return
                    command = f".改换星移 @{STAR_SHIFT_TARGET}"
                    if not self.common_star_gazing_reply_target_matches_identity(resp_msg.id, avatar, logger=log):
                        return
                    if not self.common_mark_star_shift_attempt(
                        avatar,
                        today,
                        source="active-window dispatch",
                        logger=log,
                    ):
                        return
                    self.save_state()
                    log.info(f"Avatar {avatar} Star gazing: sending {command} in ACTIVE window as reply to msg {resp_msg.id}.")
                    sent_msg = await self.send_and_wait_feedback_identity(
                        avatar,
                        command,
                        reply_to=resp_msg.id,
                        suppress_no_response_alert=True,
                    )
                    if sent_msg:
                        self.set_avatar_state(avatar, "last_star_shift_date", today)
                        self.set_avatar_state(avatar, "last_star_shift_time", now_str())
                else:
                    target_dt = manifest_dt or self.next_star_manifest_dt(datetime.now())
                    target_day = target_dt.strftime("%Y-%m-%d")
                    if self.star_gazing_final_report_seen(target_dt):
                        log.info(
                            f"Avatar {avatar} Star gazing: final report already seen for {dt_to_str(target_dt)}; "
                            "not scheduling .改换星移."
                        )
                        return
                    if self.get_avatar_state(avatar).get("last_star_shift_date") != target_day:
                        log.info(f"Avatar {avatar} Star gazing: GOOD result; scheduling .改换星移 for manifest {dt_to_str(target_dt)}.")
                        asyncio.create_task(self.avatar_schedule_star_shift(avatar, resp_msg.id, target_dt, target_day))
            else:
                log.info(f"Avatar {avatar} Star gazing: .观星 result does not contain GOOD keyword; skipping .改换星移.")
        finally:
            if self.active_atomic_task == asyncio.current_task():
                self.active_atomic_task = None
                log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarGazing-{avatar}")

    async def maybe_run_daily_star_gazing_fallback(self, now=None):
        """Send one fallback .观星 through a rotating Star Palace avatar at 23:59."""
        now = now or datetime.now()
        fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)
        if not fallback_dt or now < fallback_dt:
            return False

        async with self.star_gazing_lock:
            now = datetime.now()
            fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)
            if not fallback_dt or now < fallback_dt:
                return False

            today = now.strftime("%Y-%m-%d")
            self.state["last_star_gazing_fallback_date"] = today
            selected_avatar, _ = self.choose_star_gazing_avatar_for_today(today)
            if not selected_avatar:
                log.info(f"Avatar Star gazing fallback: all rotating avatars already observed on {today}; skipping.")
                self.save_state()
                return True

            if self.dashboard_command_paused(".观星", selected_avatar):
                log.info(f"Avatar Star gazing fallback: .观星 paused for {selected_avatar}; skipping 23:59 fallback.")
                self.clear_avatar_star_gazing_pending(selected_avatar)
                self.clear_star_gazing_round_claim()
                self.save_state()
                return True

            target_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            manifest_key = dt_to_str(target_dt)
            self.state["star_gazing_claimed_manifest_time"] = manifest_key
            self.state["star_gazing_claimed_avatar"] = selected_avatar
            self.state["pending_star_gazing_manifest_time"] = manifest_key
            self.state["pending_star_gazing_fate_type"] = "Good - daily fallback"
            self.set_avatar_state(selected_avatar, "pending_star_gazing_date", today)
            self.set_avatar_state(selected_avatar, "pending_star_gazing_target_time", dt_to_str(now))
            self.set_avatar_state(selected_avatar, "next_star_gazing_time", dt_to_str(now))
            self.save_state()

            log.info(
                f"Avatar Star gazing fallback: no .观星 today; sending .观星 as "
                f"{selected_avatar} at 23:59."
            )
            await self.avatar_schedule_star_gazing_simple(
                selected_avatar,
                now,
                immediate_shift=False,
                manifest_dt=target_dt,
                gazing_date=today,
            )
            return True

    async def run_star_gazing_loop(self):
        await self.startup_done.wait()
        while self.is_running:
            now = datetime.now()
            if await self.maybe_run_daily_star_gazing_fallback(now):
                await asyncio.sleep(5)
                continue

            fallback_dt = self.pending_daily_star_gazing_fallback_dt(now)
            if fallback_dt and now < fallback_dt:
                wait_sec = (fallback_dt - now).total_seconds()
                log.info(
                    f"Avatar Star gazing fallback waiting {int(wait_sec)}s until "
                    f"{dt_to_str(fallback_dt)}."
                )
                await asyncio.sleep(scheduler_sleep_seconds(wait_sec))
                continue

            await asyncio.sleep(scheduler_sleep_seconds(600))

    async def avatar_handle_star_gazing_opportunity(self, avatar, msg, text, sender):
        is_our_good = self.star_gazing_good_opportunity(text)
        is_any_good = bool(text and "【Good -" in text)
        fate_type = self.star_gazing_manifest_fate_type(text)
        is_manifest_notice = bool(text and "【星盘显化】" in text and fate_type)

        if not is_any_good and not is_manifest_notice: return
        if not sender or not is_game_bot_sender(self, sender): return

        now = datetime.now()
        notice_manifest_dt = self.star_gazing_manifest_for_notice(now)
        manifest_dt = notice_manifest_dt or self.star_gazing_target_for_opportunity(now)
        manifest_key = dt_to_str(manifest_dt)

        if is_manifest_notice and not is_any_good:
            async with self.star_gazing_lock:
                pending_manifest = (
                    self.state.get("pending_star_gazing_manifest_time", "")
                    or self.state.get("star_gazing_claimed_manifest_time", "")
                )
                pending_manifest_dt = str_to_dt(pending_manifest)
                current_manifest_dt = self.current_star_report_manifest_dt(now)
                claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                claimed_manifest_dt = str_to_dt(claimed_manifest)
                claimed_date = (
                    self.get_avatar_state(claimed_avatar).get("pending_star_gazing_date", "")
                    if claimed_avatar
                    else ""
                ) or now.strftime("%Y-%m-%d")
                if (
                    claimed_manifest == manifest_key
                    and claimed_manifest_dt
                    and claimed_avatar
                    and claimed_avatar in (getattr(self, "avatars", []) or [])
                    and self.common_star_gazing_observer_identity(text) == claimed_avatar
                    and self.maybe_record_passive_claimed_star_gazing_result(
                        claimed_avatar,
                        claimed_manifest_dt,
                        claimed_date,
                        msg,
                        text,
                    )
                ):
                    return
                cancels_pending_manifest = (
                    pending_manifest == manifest_key
                    or bool(pending_manifest_dt and pending_manifest_dt <= current_manifest_dt)
                )
                if pending_manifest and cancels_pending_manifest:
                    claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                    cleared = False
                    for candidate in STAR_GAZING_ROTATING_AVATARS:
                        pending = self.get_avatar_state(candidate).get("pending_star_gazing_target_time", "")
                        if pending:
                            self.clear_avatar_star_gazing_pending(candidate)
                            cleared = True
                    if claimed_avatar and not cleared:
                        self.clear_avatar_star_gazing_pending(claimed_avatar)
                        cleared = True
                    if cleared:
                        self.clear_star_gazing_round_claim()
                        self.save_state()
                        log.info(
                            f"Avatar Star gazing: CANCELLED pending .观星 for manifest "
                            f"{manifest_key}; updated fate is {fate_type}."
                        )
            return

        if is_our_good:
            if notice_manifest_dt is None:
                log.info(
                    "Avatar Star gazing: target Good notice arrived after this round's final report; "
                    "ignoring without consuming .观星."
                )
                return

            send_dt, immediate_shift, gazing_date = self.star_gazing_schedule_plan(now, manifest_dt)
            immediate_shift = True

            async with self.star_gazing_lock:
                claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                if claimed_manifest and claimed_avatar and self.claimed_star_gazing_pending_due(claimed_avatar, now):
                    claimed_manifest_dt = str_to_dt(claimed_manifest)
                    if claimed_manifest_dt:
                        manifest_dt = claimed_manifest_dt
                        manifest_key = claimed_manifest
                        gazing_date = self.get_avatar_state(claimed_avatar).get("pending_star_gazing_date", "") or gazing_date
                if claimed_manifest == manifest_key and claimed_avatar:
                    if self.maybe_record_passive_claimed_star_gazing_result(
                        claimed_avatar,
                        manifest_dt,
                        gazing_date,
                        msg,
                        text,
                    ):
                        return
                    log.info(
                        f"Avatar Star gazing: manifest {manifest_key} already assigned to {claimed_avatar}; "
                        "skip duplicate trigger."
                    )
                    return

                assigned_avatar = self.common_star_gazing_assigned_avatar_for_manifest(manifest_dt)
                if assigned_avatar:
                    log.info(
                        f"Avatar Star gazing: manifest {manifest_key} already spent by {assigned_avatar}; "
                        "skip duplicate trigger."
                    )
                    return

                selected_avatar = avatar
                idx = 0
                if not selected_avatar:
                    selected_avatar, idx = self.choose_star_gazing_avatar_for_today(gazing_date)
                if not selected_avatar and send_dt.date() < manifest_dt.date():
                    send_dt = max(manifest_dt + timedelta(seconds=3), now + timedelta(seconds=3))
                    immediate_shift = True
                    gazing_date = send_dt.strftime("%Y-%m-%d")
                    selected_avatar, idx = self.choose_star_gazing_avatar_for_today(gazing_date)
                if not selected_avatar:
                    log.info(f"Avatar Star gazing: all rotating avatars already observed on {gazing_date}; skipping.")
                    return
                if (
                    self.get_avatar_state(selected_avatar).get("last_gazing_date") == gazing_date
                    and send_dt.date() < manifest_dt.date()
                ):
                    send_dt = max(manifest_dt + timedelta(seconds=3), now + timedelta(seconds=3))
                    immediate_shift = True
                    gazing_date = send_dt.strftime("%Y-%m-%d")
                if self.get_avatar_state(selected_avatar).get("last_gazing_date") == gazing_date:
                    return

                self.state["star_gazing_claimed_manifest_time"] = manifest_key
                self.state["star_gazing_claimed_avatar"] = selected_avatar
                self.state["pending_star_gazing_manifest_time"] = manifest_key
                self.state["pending_star_gazing_fate_type"] = self.star_gazing_pending_fate_type(text)
                self.common_mark_star_gazing_round_assigned(
                    manifest_dt,
                    selected_avatar,
                    source="manifest opportunity",
                    logger=log,
                )
                self.set_avatar_state(selected_avatar, "pending_star_gazing_date", gazing_date)
                self.set_avatar_state(selected_avatar, "pending_star_gazing_target_time", dt_to_str(send_dt))
                self.set_avatar_state(selected_avatar, "next_star_gazing_time", dt_to_str(send_dt))
                self.save_state()

            avatars = STAR_GAZING_ROTATING_AVATARS
            next_idx = self.state.get("star_gazing_avatar_index", 0) % len(avatars)
            log.info(
                f"观星轮换: 显化 {dt_to_str(manifest_dt)} 指派 {selected_avatar} "
                f"(索引 {idx}), 下次轮换至 {avatars[next_idx]}"
            )
            if immediate_shift:
                log.info(
                    f"Avatar {selected_avatar} Star manifest GOOD detected DURING active window "
                    f"({dt_to_str(manifest_dt)}); sending .观星 IMMEDIATELY."
                )
            else:
                log.info(
                    f"Avatar {selected_avatar} Star manifest GOOD detected; "
                    f"scheduling .观星 at {dt_to_str(send_dt)}."
                )
            asyncio.create_task(
                self.avatar_schedule_star_gazing_simple(
                    selected_avatar,
                    send_dt,
                    immediate_shift=immediate_shift,
                    manifest_dt=manifest_dt,
                    gazing_date=gazing_date,
                )
            )
        else:
            async with self.star_gazing_lock:
                cleared = False
                for candidate in STAR_GAZING_ROTATING_AVATARS:
                    pending = self.get_avatar_state(candidate).get("pending_star_gazing_target_time", "")
                    if pending and is_future(pending):
                        self.set_avatar_state(candidate, "pending_star_gazing_date", "")
                        self.set_avatar_state(candidate, "pending_star_gazing_target_time", "")
                        self.set_avatar_state(candidate, "next_star_gazing_time", "")
                        cleared = True
                        log.info(f"Avatar {candidate} Star gazing: CANCELLED pending .观星 (was at {pending}) because of non-target keyword.")
                if cleared:
                    self.clear_star_gazing_round_claim()
                    self.save_state()

    async def restore_pending_star_gazing_after_startup(self):
        await self.startup_done.wait()
        avatar = self.state.get("star_gazing_claimed_avatar", "")
        manifest_text = (
            self.state.get("pending_star_gazing_manifest_time", "")
            or self.state.get("star_gazing_claimed_manifest_time", "")
        )
        if self.cleanup_stale_star_gazing_pending(active_avatar=avatar, manifest_text=manifest_text):
            self.save_state()
        if not avatar or not manifest_text:
            return
        pending_fate_type = self.state.get("pending_star_gazing_fate_type", "")
        if "Good" not in pending_fate_type:
            log.info(
                f"Avatar Star gazing: clearing pending .观星 for {manifest_text}; "
                "missing confirmed Good fate marker."
            )
            self.set_avatar_state(avatar, "pending_star_gazing_date", "")
            self.set_avatar_state(avatar, "pending_star_gazing_target_time", "")
            self.set_avatar_state(avatar, "next_star_gazing_time", "")
            self.clear_star_gazing_round_claim()
            self.save_state()
            return

        cleared_stale_avatar_pending = False
        manifest_dt_for_restore = str_to_dt(manifest_text) if manifest_text else None
        latest_send_dt_for_restore = (
            manifest_dt_for_restore - timedelta(seconds=60)
            if manifest_dt_for_restore
            else None
        )
        for candidate in STAR_GAZING_ROTATING_AVATARS:
            if candidate == avatar:
                continue
            candidate_state = self.get_avatar_state(candidate)
            candidate_pending = candidate_state.get("pending_star_gazing_target_time", "")
            if not candidate_pending:
                continue
            self.set_avatar_state(candidate, "pending_star_gazing_date", "")
            self.set_avatar_state(candidate, "pending_star_gazing_target_time", "")
            self.set_avatar_state(candidate, "next_star_gazing_time", "")
            cleared_stale_avatar_pending = True
        if cleared_stale_avatar_pending:
            self.save_state()

        avatar_state = self.get_avatar_state(avatar)
        pending_send = avatar_state.get("pending_star_gazing_target_time", "")
        send_dt = str_to_dt(pending_send) if pending_send else None
        pending_send_is_valid = bool(
            send_dt
            and is_future(pending_send)
            and (not latest_send_dt_for_restore or send_dt <= latest_send_dt_for_restore)
        )
        if not pending_send_is_valid:
            if pending_send:
                log.info(
                    f"Avatar {avatar} Star gazing: clearing expired pending .观星; "
                    f"send={pending_send}, manifest={manifest_text}."
                )
            self.set_avatar_state(avatar, "pending_star_gazing_date", "")
            self.set_avatar_state(avatar, "pending_star_gazing_target_time", "")
            self.set_avatar_state(avatar, "next_star_gazing_time", "")
            self.clear_star_gazing_round_claim()
            self.save_state()
            return

        manifest_dt = manifest_dt_for_restore
        preferred_send_dt = manifest_dt - timedelta(
            seconds=max(60, int(STAR_GAZING_COMMAND_LEAD_SECONDS))
        ) if manifest_dt else None
        if preferred_send_dt and preferred_send_dt > datetime.now() and send_dt != preferred_send_dt:
            send_dt = preferred_send_dt
            pending_send = dt_to_str(send_dt)
            self.set_avatar_state(avatar, "pending_star_gazing_target_time", pending_send)
            self.set_avatar_state(avatar, "next_star_gazing_time", pending_send)
            self.save_state()
        pending_date = avatar_state.get("pending_star_gazing_date", "") or send_dt.strftime("%Y-%m-%d")
        if avatar_state.get("last_gazing_date") == pending_date:
            log.info(
                f"Avatar {avatar} Star gazing: clearing pending .观星; "
                f"already observed on {pending_date}."
            )
            self.set_avatar_state(avatar, "pending_star_gazing_date", "")
            self.set_avatar_state(avatar, "pending_star_gazing_target_time", "")
            self.set_avatar_state(avatar, "next_star_gazing_time", "")
            self.clear_star_gazing_round_claim()
            self.save_state()
            return

        log.info(
            f"Avatar {avatar} Star gazing: restoring pending .观星 at "
            f"{pending_send}, manifest {manifest_text}."
        )
        restore_immediate_shift = False
        asyncio.create_task(
            self.avatar_schedule_star_gazing_simple(
                avatar,
                send_dt,
                immediate_shift=restore_immediate_shift,
                manifest_dt=manifest_dt,
                gazing_date=pending_date,
            )
        )

    # ---- 分身星辰牵引 / 安抚 / 收集 ----

    def response_text(self, resp):
        return self.common_response_text(resp)

    def record_avatar_taiyi_guide_response(self, avatar, resp, source=TAIYI_GUIDE_COMMAND):
        """记录太一门引道回执；成功按 12 小时冷却，冷却提示按回执时间排程。"""
        text = self.response_text(resp)
        now = now_str()
        updates = {
            "last_taiyi_guide_response": (text or "")[:500],
        }
        clean = str(text or "").replace("**", "")

        if not clean:
            updates["next_taiyi_guide_time"] = add_seconds_str(now, TAIYI_GUIDE_RETRY_SECONDS)
            self.update_avatar_states(avatar, updates)
            log.warning(f"Avatar [{avatar}] Taiyi guide: no response; retry in {TAIYI_GUIDE_RETRY_SECONDS}s.")
            return "empty"

        cd = self.parse_wait_time(clean)
        cooldown_keywords = ("冷却", "后再", "还需", "尚需", "间隔", "稍后", "请在")
        if cd > 0 and any(keyword in clean for keyword in cooldown_keywords):
            updates["next_taiyi_guide_time"] = add_seconds_str(now, cd + 60)
            self.update_avatar_states(avatar, updates)
            log.info(f"Avatar [{avatar}] Taiyi guide: cooldown {cd}s from {source}.")
            return "cooldown"

        blocked_keywords = (
            "非太一门",
            "不是太一门",
            "并非太一门",
            "不属于太一门",
            "无法引道",
            "不能引道",
            "修为不足",
        )
        if any(keyword in clean for keyword in blocked_keywords):
            updates["next_taiyi_guide_time"] = add_seconds_str(now, 3600)
            self.update_avatar_states(avatar, updates)
            notify_unrecognized_response(self, TAIYI_GUIDE_COMMAND, text, log, f"太一门引道/{avatar}/{source}")
            log.warning(f"Avatar [{avatar}] Taiyi guide blocked; retry in 1h.")
            return "blocked"

        updates.update({
            "last_taiyi_guide_time": now,
            "next_taiyi_guide_time": add_seconds_str(now, TAIYI_GUIDE_CD_SECONDS),
        })
        self.update_avatar_states(avatar, updates)
        log.info(f"Avatar [{avatar}] Taiyi guide recorded; next at {updates['next_taiyi_guide_time']}.")
        return "success"

    async def run_avatar_taiyi_guide_loop(self, avatar, initial_delay=0):
        """太一门缘生子专属引道循环：.引道 水，12 小时冷却。"""
        await self.startup_done.wait()
        self._avatar_loop_count += 1
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        while self.is_running:
            avatar = self.resolve_avatar_identity(avatar)
            try:
                pause_left = self.identity_pause_seconds(avatar)
                if pause_left > 0:
                    await asyncio.sleep(scheduler_sleep_seconds(pause_left, minimum=60))
                    continue
                state = self.get_avatar_state(avatar)
                next_time = state.get("next_taiyi_guide_time", "")
                if next_time and is_future(next_time):
                    await asyncio.sleep(scheduler_sleep_seconds(min(seconds_until(next_time), 1800), minimum=30))
                    continue
                if self.dashboard_command_paused(TAIYI_GUIDE_COMMAND, avatar):
                    await asyncio.sleep(scheduler_sleep_seconds(600))
                    continue

                async with AtomicTaskContext(self, f"TaiyiGuide-{avatar}"):
                    state = self.get_avatar_state(avatar)
                    next_time = state.get("next_taiyi_guide_time", "")
                    if next_time and is_future(next_time):
                        continue
                    resp = await self.send_and_wait_feedback_identity(
                        avatar,
                        TAIYI_GUIDE_COMMAND,
                        timeout=60,
                        max_retries=1,
                        force_identity_check=True,
                    )
                    self.record_avatar_taiyi_guide_response(avatar, resp)

                state = self.get_avatar_state(avatar)
                next_time = state.get("next_taiyi_guide_time", "")
                sleep_for = min(seconds_until(next_time), 1800) if next_time and is_future(next_time) else 600
                await asyncio.sleep(scheduler_sleep_seconds(sleep_for, minimum=30))
            except Exception as e:
                log.error(f"Avatar [{avatar}] Taiyi guide loop error: {e}", exc_info=True)
                self.set_avatar_state(avatar, "next_taiyi_guide_time", add_seconds_str(now_str(), TAIYI_GUIDE_RETRY_SECONDS))
                await asyncio.sleep(scheduler_sleep_seconds(60))

    def recent_command_guard_wait(self, command="", max_age_seconds=15):
        return self.common_recent_command_guard_wait(command, max_age_seconds=max_age_seconds)

    def apply_avatar_star_guard_backoff(self, avatar, command="", fields=None, reason="command guard"):
        return self.common_apply_avatar_star_guard_backoff(
            avatar,
            command=command,
            fields=fields,
            reason=reason,
            logger=log,
            log_level="warning",
            include_reason=False,
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
        resp = await self.send_and_wait_feedback_identity(avatar, ".观星台", timeout=45, max_retries=1, force_identity_check=True)
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
            await self._avatar_settle_and_start_deep(avatar)
        except Exception as e:
            log.error(f"Avatar [{avatar}] failed to restart deep meditation after star action: {e}")

    async def attempt_avatar_star_pull(self, avatar):
        resp = await self.send_and_wait_feedback_identity(
            avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1, force_identity_check=True
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
        await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=45, max_retries=1, force_identity_check=True)
        self.update_avatar_states(avatar, {
            "in_deep_meditation": False,
            "deep_meditation_end_time": "",
        })
        await asyncio.sleep(3)

        retry_resp = await self.send_and_wait_feedback_identity(
            avatar, STAR_ATTRACTION_COMMAND, timeout=60, max_retries=1, force_identity_check=True
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
            avatar, ".安抚星辰", timeout=45, max_retries=1, force_identity_check=True
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
            avatar, ".收集精华", timeout=45, max_retries=1, force_identity_check=True
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
        """素心子/缘生子的观星台牵引循环：牵引 -> 安抚 -> 收集 -> 再牵引。"""
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

    # ---- 主循环：仍保留的群内灵兽巡边 ----

    async def run_beast_action_timer(self):
        """
        仅保留 Mini App 暂未提供接口的灵兽巡边群指令。
        """
        await self.startup_done.wait()
        while self.is_running:
            if await self.sleep_if_main_soul_paused("Beast action loop"):
                continue
            # 化身正在发送命令时等待，避免以错误身份发送灵兽命令
            if self.avatar_send_lock.locked():
                log.info("Beast timer deferred: avatar_send_lock held. Waiting 30s.")
                await asyncio.sleep(30)
                continue
            sleep_for = 600
            async with self.beast_lock:
                self.repair_overdue_beast_border_patrol_schedule()
                last_patrol = self.state.get("last_beast_border_patrol_time", "")
                next_patrol = self.state.get("next_beast_border_patrol_time", "")
                need_patrol = not next_patrol or not is_future(next_patrol)
                if need_patrol and last_patrol:
                    need_patrol = not is_future(add_seconds_str(last_patrol, BEAST_BORDER_PATROL_CD_SECONDS))
                if need_patrol:
                    async with AtomicTaskContext(self, "BeastBorderPatrol"):
                        patrol_mode = self.configured_beast_border_patrol_mode()
                        log.info(f"Beast border patrol due: sending configured mode {patrol_mode}.")
                        await self.run_beast_border_patrol(patrol_mode)
                        self.save_state()
                    sleep_for = 30
                elif next_patrol and is_future(next_patrol):
                    sleep_for = max(30, seconds_until(next_patrol) + random.randint(10, 30))
            await self.sleep_beast_action(sleep_for)

    # ---- 闭关循环 ----

    async def recall_concubine_before_meditation_end(self):
        log.info("Meditation: .召回侍妾 disabled; skipping concubine recall.")
        self.state["concubine_recalled_for_meditation"] = False; self.state["concubine_recalled_time"] = ""; self.save_state()

    async def place_concubine_after_meditation_start(self):
        if not self.state.get("concubine_recalled_for_meditation"): return
        log.info("Meditation: Deep meditation started, sending .安置侍妾.")
        await self.send_and_wait_feedback(".安置侍妾"); self.state["concubine_recalled_for_meditation"] = False; self.state["concubine_recalled_time"] = ""; self.save_state()

    async def sleep_until_meditation_check(self, end_time, label="Meditation"):
        protected_until = self.meditation_protected_until({"deep_meditation_end_time": end_time}) or end_time
        remaining = seconds_until(protected_until)
        if remaining <= 0: return
        wait_sec = remaining + random.randint(10, 30)
        if wait_sec > 0:
            await asyncio.sleep(scheduler_sleep_seconds(wait_sec))

    async def run_meditation_timer(self):
        """深度闭关循环：查看状态→结算→重新开始"""
        await self.startup_done.wait()
        while self.is_running:
            if await self.sleep_if_main_soul_paused("Meditation loop"):
                continue
            retry_time = self.meditation_defer_until(self.state)
            if retry_time and is_future(retry_time):
                await asyncio.sleep(scheduler_sleep_seconds(seconds_until(retry_time) + random.randint(10, 30))); continue
            if self.ensure_meditation_guard_from_end_time(self.state):
                self.save_state()
            guard_time = self.state.get("deep_meditation_guard_until", "")
            if guard_time and is_future(guard_time):
                await asyncio.sleep(scheduler_sleep_seconds(seconds_until(guard_time) + random.randint(10, 30))); continue
            end_time = self.state.get("deep_meditation_end_time", "")
            if self.state.get("in_deep_meditation") and end_time and is_future(end_time):
                await self.sleep_until_meditation_check(end_time); continue

            async def settle_and_start_deep():
                cultivation_resp = await self.send_and_wait_feedback(".闭关修炼")
                retry_cd = self.defer_meditation_after_cultivation_cooldown(
                    "主魂", cultivation_resp, "Main meditation .闭关修炼"
                )
                if retry_cd:
                    return retry_cd + random.randint(10, 30)
                await asyncio.sleep(3)
                med_resp = self.response_text(await self.send_and_wait_feedback(".深度闭关")); cd_med = self.parse_wait_time(med_resp)
                if any(k in med_resp for k in ["冷却", "后再试", "无法立即", "尚未平复"]):
                    if cd_med > 0: self.state["in_deep_meditation"] = False; self.state["deep_meditation_guard_until"] = ""; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), cd_med); return cd_med + random.randint(10, 30)
                if any(k in med_resp for k in ["已进入", "深度闭关", "已在", "开启", "成功"]):
                    end_time = add_seconds_str(now_str(), cd_med if cd_med > 0 else 8 * 3600)
                    self.state.update(self.meditation_active_state_values(end_time, clear_restart=False)); self.save_state()
                    await self.place_concubine_after_meditation_start(); return 300
                if med_resp: notify_unrecognized_response(self, ".深度闭关", med_resp, log, "深度闭关")
                self.state["in_deep_meditation"] = False; self.state["deep_meditation_guard_until"] = ""; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600); self.save_state(); return 600

            resp = await self.send_and_wait_feedback(".查看闭关")
            resp_text = self.response_text(resp)
            cd = self.parse_wait_time(resp_text)
            next_sleep = 300
            if cd > 0:
                end_time = add_seconds_str(now_str(), cd)
                self.state.update(self.meditation_active_state_values(end_time, clear_restart=False)); self.save_state(); await self.sleep_until_meditation_check(self.state["deep_meditation_end_time"]); continue
            elif is_deep_meditation_settlement_response(resp_text): next_sleep = await settle_and_start_deep()
            elif is_not_deep_meditation_response(resp_text): next_sleep = await settle_and_start_deep()
            elif is_deep_meditation_ongoing_response(resp_text): self.state["in_deep_meditation"] = True; self.state["next_meditation_retry_time"] = ""; self.state["next_meditation_time"] = ""; self.save_state(); await asyncio.sleep(scheduler_sleep_seconds(600)); continue
            else:
                if resp_text: notify_unrecognized_response(self, ".查看闭关", resp_text, log, "闭关状态")
                self.state["in_deep_meditation"] = False; self.state["deep_meditation_guard_until"] = ""; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600); self.save_state(); next_sleep = 600
            self.save_state(); await asyncio.sleep(scheduler_sleep_seconds(next_sleep))

    # ---- 身外化身：分身闭关循环 ----

    def is_avatar_deep_meditation_start_success(self, text):
        clean = str(text or "").replace("**", "")
        return (
            any(k in clean for k in ["成功", "开启", "已进入深度闭关", "深度闭关状态", "神魂将自行吐纳"])
            or ("已在" in clean and "深度闭关" in clean)
        )

    async def record_avatar_deep_meditation_start(self, avatar, response_text):
        if not self.is_avatar_deep_meditation_start_success(response_text):
            self.update_avatar_states(avatar, {
                "in_deep_meditation": False,
                "deep_meditation_guard_until": "",
                "meditation_restart_pending": True,
                "next_meditation_retry_time": add_seconds_str(now_str(), 600),
            })
            return False

        cd = self.parse_wait_time(response_text)
        if cd <= 0:
            log.warning(
                f"Avatar [{avatar}] deep meditation start confirmed without duration; "
                "using 8h fallback without .查看闭关 verification."
            )

        end_time = add_seconds_str(now_str(), cd if cd > 0 else 8 * 3600)
        self.update_avatar_states(avatar, self.meditation_active_state_values(end_time))
        return True

    async def run_avatar_meditation_loop(self, avatar, initial_delay=0):
        """
        分身闭关独立循环。
        对指定分身执行：.查看闭关 → .闭关修炼 → .深度闭关 的 8 小时闭关结算环。
        每个分身有独立的冷却状态，存储在 state.avatars[avatar] 中。
        """
        await self.startup_done.wait()
        self._avatar_loop_count += 1
        if initial_delay > 0:
            log.info(f"Avatar [{avatar}] meditation loop: waiting {initial_delay}s before start...")
            await asyncio.sleep(initial_delay)

        while self.is_running:
            try:
                a_state = self.get_avatar_state(avatar)

                # 检查重试冷却
                retry_time = self.meditation_defer_until(a_state)
                if retry_time and is_future(retry_time):
                    await asyncio.sleep(min(seconds_until(retry_time), 300) + random.randint(10, 30))
                    continue

                if self.ensure_meditation_guard_from_end_time(a_state):
                    self.save_state()
                end_time = a_state.get("deep_meditation_end_time", "")
                if a_state.get("meditation_restart_pending") and end_time and is_future(end_time):
                    a_state["meditation_restart_pending"] = False
                    a_state["meditation_restart_mode"] = ""
                    self.save_state()
                    log.info(
                        f"Avatar [{avatar}] ignored stale meditation_restart_pending; "
                        f"cached deep meditation runs until {end_time}."
                    )
                guard_wait = self.meditation_guard_wait_seconds_for_state(a_state)
                if guard_wait > 0:
                    log.debug(
                        f"Avatar [{avatar}] deep meditation protected for "
                        f"{self.compact_duration_text(guard_wait)}; skipping .查看闭关."
                    )
                    await asyncio.sleep(scheduler_sleep_seconds(guard_wait + random.randint(10, 30)))
                    continue

                if a_state.get("meditation_restart_mode") == "deep_only":
                    log.info(f"Avatar [{avatar}] direct deep meditation restart pending; sending .深度闭关.")
                    async with self.common_atomic_task(f"Meditation-{avatar}"):
                        deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                        deep_text = self.response_text(deep_resp)
                        started = await self.record_avatar_deep_meditation_start(avatar, deep_text)
                    await asyncio.sleep(60 if started else 600)
                    continue

                # 检查闭关中
                end_time = a_state.get("deep_meditation_end_time", "")
                if a_state.get("meditation_restart_pending"):
                    end_time = ""
                    log.info(f"Avatar [{avatar}] meditation restart pending; checking immediately.")
                if a_state.get("in_deep_meditation") and end_time and is_future(end_time):
                    protected_until = self.meditation_protected_until(a_state) or end_time
                    wait_sec = seconds_until(protected_until) + random.randint(10, 30)
                    log.info(
                        f"Avatar [{avatar}] in deep meditation until {end_time}; "
                        f"protected until {protected_until}. Sleeping {wait_sec}s."
                    )
                    await asyncio.sleep(scheduler_sleep_seconds(wait_sec))
                    continue

                log.info(f"Avatar [{avatar}] meditation check: sending .查看闭关")
                result = await self.run_avatar_meditation_restart_chain(
                    avatar,
                    source="meditation loop",
                    check_timeout=30,
                    check_retries=1,
                    cultivation_timeout=45,
                    cultivation_retries=1,
                    deep_timeout=60,
                    deep_retries=1,
                )
                status = result.get("status")
                if status == "ongoing":
                    guard_wait = self.meditation_guard_wait_seconds_for_state(self.get_avatar_state(avatar))
                    await asyncio.sleep(scheduler_sleep_seconds(guard_wait + random.randint(10, 30)))
                    continue
                if status == "ongoing_unknown":
                    self.update_avatar_states(avatar, {
                        "in_deep_meditation": True,
                        "next_meditation_retry_time": "",
                        "meditation_restart_pending": False,
                    })
                    await asyncio.sleep(300)
                    continue
                if status in {"started", "failed", "deferred"}:
                    wait_seconds = int(result.get("wait") or (60 if status == "started" else 600))
                    if status == "deferred":
                        wait_seconds += random.randint(10, 30)
                    await asyncio.sleep(scheduler_sleep_seconds(wait_seconds))
                    continue
                resp_text = result.get("text", "")
                if resp_text:
                    log.warning(f"Avatar [{avatar}] unrecognized .查看闭关 response: {resp_text[:100]}")
                    self.set_avatar_state(avatar, "in_deep_meditation", False)
                    self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                    self.set_avatar_state(avatar, "meditation_restart_pending", True)
                else:
                    log.warning(f"Avatar [{avatar}] empty response for .查看闭关 (timeout/rate-limited). Preserving state and retrying in 10m.")
                self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), 600))
                await asyncio.sleep(scheduler_sleep_seconds(600))
                continue

            except Exception as e:
                log.error(f"Avatar [{avatar}] meditation loop error: {e}", exc_info=True)
                await asyncio.sleep(300)

    async def _avatar_settle_and_start_deep(self, avatar, initial_check_text=None):
        """分身闭关结算并重新开始深度闭关。先用 .查看闭关 获取准确状态。"""
        result = await self.run_avatar_meditation_restart_chain(
            avatar,
            initial_check_text=initial_check_text,
            check_first=True,
            source="settle restart",
            check_timeout=30,
            check_retries=1,
            cultivation_timeout=45,
            cultivation_retries=1,
            deep_timeout=60,
            deep_retries=1,
        )
        status = result.get("status")
        if status == "ongoing":
            return max(60, int(result.get("wait") or 300)) + random.randint(10, 30)
        if status == "ongoing_unknown":
            guard_wait = self.meditation_guard_wait_seconds_for_state(self.get_avatar_state(avatar))
            return max(60, int(guard_wait or 300)) + random.randint(10, 30)
        if status == "deferred":
            return int(result.get("wait") or 600) + random.randint(10, 30)
        if status == "started":
            log.info(f"Avatar [{avatar}] deep meditation started until {self.get_avatar_state(avatar).get('deep_meditation_end_time', '')}")
            return 300
        if status == "unknown_check":
            text = result.get("text", "")
            if text:
                log.warning(f"Avatar [{avatar}] unrecognized .查看闭关 response: {text[:100]}")
        self.set_avatar_state(avatar, "in_deep_meditation", False)
        self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), 600))
        self.set_avatar_state(avatar, "meditation_restart_pending", True)
        return 600

    # ============================================================
    # 化身日常与星宫相关循环
    # ============================================================

    async def _avatar_mulan_support(self, avatar):
        return await self.common_avatar_mulan_support(avatar)

    @safe_bg_task
    async def delayed_avatar_force_exit(self, avatar, delay_sec):
        try:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            log.info(f"Avatar {avatar}: force-exit timer elapsed. Forcing exit.")
            async with AtomicTaskContext(self, f"AvatarForceExit-{avatar}"):
                await self.send_and_wait_feedback_identity(avatar, ".强行出关", timeout=60)
                self.update_avatar_states(avatar, {
                    "in_deep_meditation": False,
                    "deep_meditation_end_time": "",
                    "next_force_exit_time": "",
                    "meditation_restart_pending": True,
                    "meditation_restart_mode": "deep_only",
                })
                await asyncio.sleep(3)
                deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关", timeout=60)
                await self.record_avatar_deep_meditation_start(avatar, self.response_text(deep_resp))
        except Exception as e:
            log.error(f"Avatar {avatar} delayed_force_exit error: {e}")
            self.set_avatar_state(avatar, "next_force_exit_time", "")

    async def run_avatar_star_palace_loop(self, avatar, initial_delay=0):
        """化身日常循环：慕兰支援与仍有效的侍妾链。"""
        await self.startup_done.wait()  # 新增：等待启动对账完成，杜绝死锁
        self._avatar_loop_count += 1
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        async def send_with_cultivation_check(cmd, reply_to=None, **kwargs):
            resp = await self.send_and_wait_feedback_identity(avatar, cmd, reply_to=reply_to, **kwargs)
            
            def get_text(r):
                return getattr(r, "text", "") if hasattr(r, "text") else str(r) if isinstance(r, str) else ""
                
            forced_exit = False
            if get_text(resp) and "修为不足" in get_text(resp):
                log.info(f"Avatar {avatar}: cultivation insufficient for {cmd}, trying to force exit meditation.")
                await self.send_and_wait_feedback_identity(avatar, ".强行出关")
                await asyncio.sleep(2)
                resp = await self.send_and_wait_feedback_identity(avatar, cmd, reply_to=reply_to, **kwargs)
                forced_exit = True
                if get_text(resp) and "修为不足" in get_text(resp):
                    resp = "PAUSE_1H"
            return resp, forced_exit
            
        while self.is_running:
            try:
                forced_exit = False  # 初始化：用于异常时判断是否需要恢复深度闭关
                log.info(f"DEBUG: run_avatar_star_palace_loop iteration for {avatar}")
                state = self.get_avatar_state(avatar)

                # --- 星辰牵引/安抚/收集由 run_avatar_star_attraction_loop 独立调度 ---

                # --- 周天星斗大阵 ---
                # 小号星宫分身不再主动启阵，只实时助阵副号三分身的邀请。

                # --- 侍妾批次：远航归来 -> 天机代卜 -> 入梦寻图 -> 侍妾远航 ---
                await self.execute_avatar_concubine_chain(
                    avatar,
                    send_with_cultivation_check=send_with_cultivation_check,
                )

                await self._avatar_mulan_support(avatar)

            except Exception as e:
                log.error(f"Error in avatar {avatar} daily avatar loop: {e}", exc_info=True)
                # M3: 如果已强行出关但异常中断，恢复深度闭关避免化身空转
                if forced_exit:
                    try:
                        deep_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                        await self.record_avatar_deep_meditation_start(avatar, self.response_text(deep_resp))
                        log.info(f"Avatar {avatar}: restored deep meditation after exception.")
                    except Exception as e2:
                        log.error(f"Avatar {avatar}: failed to restore deep meditation: {e2}")

            await asyncio.sleep(300)

    def start_miniapp_scheduler_tasks(self):
        """Register Mini App-backed background loops used by the XiaoHao account."""
        miniapp_router = getattr(self, "_miniapp_command_router", None)
        if miniapp_router is not None:
            star_identities = miniapp_router.star_farm_identities()
            for identity in star_identities:
                self.create_scheduler_task(
                    f"miniapp_star_farm_{identity}",
                    lambda identity=identity: miniapp_router.run_star_farm_loop(identity),
                )
        if getattr(self, "_miniapp_inventory", None) is not None:
            self.create_scheduler_task(
                "miniapp_inventory",
                lambda: self._miniapp_inventory.run_loop(),
            )
        if getattr(self, "_miniapp_fishing", None) is not None and self._miniapp_fishing.supported:
            self.create_scheduler_task(
                "miniapp_fishing",
                lambda: self._miniapp_fishing.run_loop(),
            )
        if self._miniapp_beast_contract.enabled:
            self.create_scheduler_task(
                "beast_contract",
                lambda: self._miniapp_beast_contract.run(),
            )
        if self._miniapp_beast_abyss.enabled:
            self.create_scheduler_task(
                "miniapp_beast_abyss",
                lambda: self._miniapp_beast_abyss.run_loop(),
            )
        if self._miniapp_beast_seek.enabled:
            self.create_scheduler_task(
                "miniapp_beast_seek",
                lambda: self._miniapp_beast_seek.run_loop(),
            )
        if self._miniapp_daily_activities is not None:
            if self._miniapp_daily_activities.pagoda_enabled:
                self.create_scheduler_task(
                    "miniapp_pagoda",
                    lambda: self._miniapp_daily_activities.run_pagoda_loop(),
                )
            if self._miniapp_daily_activities.hunt_enabled:
                self.create_scheduler_task(
                    "miniapp_hunt",
                    lambda: self._miniapp_daily_activities.run_hunt_loop(),
                )
            self.create_scheduler_task(
                "miniapp_tianji_trial",
                lambda: self._miniapp_daily_activities.run_tianji_trial_loop(),
            )

    # ---- 启动 ----

    async def start(self):
        """脚本入口：连接 Telegram、注册事件处理器、启动所有循环"""
        await self.client.start()
        self._miniapp_beast_contract = MiniAppBeastContractWorker.from_actor(
            self,
            logger=log,
        )
        self._miniapp_daily_activities = (
            MiniAppDailyActivities(
                self,
                self._miniapp_beast_contract.transport,
                self.account_key,
                log,
            )
            if self._miniapp_beast_contract.transport is not None
            else None
        )
        self._miniapp_beast_abyss = MiniAppBeastAbyssWorker(
            self,
            self._miniapp_beast_contract.transport,
            self.account_key,
            log,
        )
        self._miniapp_beast_seek = MiniAppBeastSeekWorker(
            self,
            self._miniapp_beast_contract.transport,
            self.account_key,
            log,
        )
        self._miniapp_inventory = (
            MiniAppInventoryWorker(
                self,
                self._miniapp_beast_contract.transport,
                self.account_key,
                log,
            )
            if self._miniapp_beast_contract.transport is not None
            else None
        )
        self._miniapp_fishing = (
            MiniAppFishingAutomation(
                self,
                self._miniapp_beast_contract.transport,
                self.account_key,
                log,
            )
            if self._miniapp_beast_contract.transport is not None
            else None
        )
        await resolve_actor_target_chats(self, log)
        await self.client.get_dialogs(limit=10)
        self.my_info = await self.client.get_me()
        log.info(f"XiaoHao Login: {self.my_info.first_name}")
        await install_red_packet_monitor(self.client, self.account_key, logger=log)
        # Reuse the transport owned by the XiaoHao Mini App schedulers. The
        # router only replaces eligible group commands; those schedulers stay
        # owned and started by this worker.
        miniapp_router = await install_miniapp_command_router(
            self,
            self.account_key,
            logger=log,
            transport=self._miniapp_beast_contract.transport,
            start_background_tasks=False,
        )
        await install_world_boss_monitor(
            self,
            self.account_key,
            logger=log,
            transport=miniapp_router.transport,
        )
        @self.client.on(events.NewMessage(chats=self.target_chat_ids))
        @routed_telegram_event_handler
        async def h(e): await self.handle_game_response(e)
        @self.client.on(events.MessageEdited(chats=self.target_chat_ids))
        @routed_telegram_event_handler
        async def eh(e):
            await log_edited_message_if_needed(self, e)
            try:
                msg = e.message
                text = msg.text or ""
                sender = await e.get_sender()
                if is_game_bot_sender(self, sender):
                    record_game_bot_activity(self, sender, log, msg=msg, text=text)
                    record_star_gazing_event("xiaohao", msg, text, sender=sender, is_edited=True, logger=log)
                    self.record_star_gazing_final_report_if_needed(msg, text, source="edited message")
                    self.record_star_shift_attempt_if_needed(msg, text, source="edited message")
                    self.maybe_record_daily_reward_from_edited_message(msg, text, source="edited message")
                    # 编辑后出现元婴遁逃·虚弱 → 立刻告警并停止脚本（防漏检补丁，加入账号强匹配）
                    if self.is_rift_weakness_response(text) and is_edited_message_for_current_account(self, msg, text):
                        identity = tracked_command_identity_for_reply(self, msg) or getattr(self, "current_identity", "主魂")
                        log.critical(f"Rift weakness DETECTED in edited message for [{identity}].\n{text}")
                        await self.stop_for_rift_weakness(text, identity=identity, msg=msg)
                        return
                    await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                    self.maybe_record_avatar_passive_states(msg)
                    # 编辑消息也能触发 feedback_events（bot 通过编辑回复指令，如共历心劫）
                    is_matched = match_pending_feedback_by_reply(
                        self, msg, text, self.is_loose_meditation_feedback_candidate, log,
                        label="[EDITED-FEEDBACK]", sender=sender
                    )
                    if not is_matched:
                        is_matched = match_pending_feedback_by_message_id(
                            self, msg, text, self.is_loose_meditation_feedback_candidate, log,
                            label="[EDITED-FEEDBACK]", sender=sender
                        )
                    # 3. 宽松匹配：有待处理事件且来自游戏bot
                    if not is_matched:
                        is_matched = match_pending_edited_feedback(
                            self,
                            msg,
                            text,
                            lambda command, body: (
                                self.is_loose_meditation_feedback_candidate(command, body)
                                or (command == ".一键放养" and self.is_no_resting_pasture_response(body))
                            ),
                            log,
                            id_window=30,
                            sender=sender,
                        )
            except Exception as ex:
                log.error(f"Edited message handler error: {ex}")
            await self.handle_pasture_return_event(e)

        async def startup_sync():
            """启动后对账闭关与仍保留的灵兽巡边状态。"""
            await asyncio.sleep(25)
            log.info("Startup Sync: Smart check for stale data...")
            
            # 重启前可能已有未完成切换或客户端断线；启动后必须重新用 .切换 主魂确认。
            self._main_confirmed = False
            log.info(
                "Startup Sync: Keep persisted identity "
                f"{getattr(self, '_persisted_identity', 'unknown')}; live identity will be re-confirmed before commands."
            )
            if self.identity_pause_seconds("主魂") > 0:
                log.info(
                    "Startup Sync: main soul is paused; skipping main-soul startup checks "
                    "and releasing avatar loops."
                )
                self.startup_done.set()
                return
            await asyncio.sleep(3)
            async with self.beast_lock:
                self.repair_overdue_beast_border_patrol_schedule()
            if self.ensure_meditation_guard_from_end_time(self.state):
                self.save_state()
            guard_wait = self.meditation_guard_wait_seconds_for_state(self.state)
            end_med = self.state.get("deep_meditation_end_time", "")
            if guard_wait > 0:
                log.info(
                    f"Startup Sync: Local meditation protected until "
                    f"{self.meditation_protected_until(self.state)}. Skipping .查看闭关."
                )
            else:
                resp_med = await self.send_and_wait_feedback(".查看闭关")
                if resp_med:
                    resp_med_text = self.response_text(resp_med)
                    cd_med = self.parse_wait_time(resp_med_text)
                    if cd_med > 0:
                        end_time = add_seconds_str(now_str(), cd_med)
                        self.state.update(self.meditation_active_state_values(end_time, clear_restart=False))
                    elif is_deep_meditation_settlement_response(resp_med_text) or is_not_deep_meditation_response(resp_med_text):
                        self.state["in_deep_meditation"] = False
                        self.state["deep_meditation_end_time"] = ""
                        self.state["deep_meditation_guard_until"] = ""
            self.save_state(); self.startup_done.set(); log.info("Startup Sync: Finished. All loops released.")
        asyncio.create_task(startup_sync())
        asyncio.create_task(resume_pending_exchange_events(self))
        asyncio.create_task(self.restore_pending_star_gazing_after_startup())
        asyncio.create_task(periodic_log_prune(LOG_FILE))
        asyncio.create_task(self.run_telegram_write_permission_monitor())
        asyncio.create_task(self.run_health_watchdog_loop())

        # 启动所有定时任务
        self.create_scheduler_task("daily_support", lambda: self.run_daily_support_tasks())
        self.create_scheduler_task("beast_action", lambda: self.run_beast_action_timer())
        self.start_miniapp_scheduler_tasks()
        self.create_scheduler_task("meditation", lambda: self.run_meditation_timer())
        self.create_scheduler_task("concubine", lambda: self.run_concubine_loop())
        self.create_scheduler_task("sect_war", lambda: self.run_sect_war_loop())
        self.create_scheduler_task("duel", lambda: self.run_duel_scheduler(initial_delay=25))
        self.create_scheduler_task("custom_command", lambda: self.run_custom_command_loop())
        self.create_scheduler_task("daily_reward_summary", lambda: self.run_daily_reward_summary_loop(initial_delay=40))
        self.create_scheduler_task("treasure_touch", lambda: self.run_treasure_touch_loop())
        self.create_scheduler_task("yuanying_out", lambda: self.run_yuanying_out_loop())
        self.create_scheduler_task("rift_search", lambda: self.run_rift_search_loop())
        self.create_scheduler_task("star_gazing", lambda: self.run_star_gazing_loop())
        self.create_scheduler_task("soul_curse", lambda: self.run_soul_curse_loop(initial_delay=100, sleep_func=scheduler_sleep_seconds))

        # 身外化身：为每个分身启动独立闭关及宗门能力循环。
        for avatar in self.avatars:
            self.create_scheduler_task(f"avatar_meditation_{avatar}", lambda avatar=avatar: self.run_avatar_meditation_loop(avatar, initial_delay=0))
            if avatar in AVATAR_YUANYING_RIFT_AVATARS:
                self.create_scheduler_task(f"avatar_yuanying_rift_{avatar}", lambda avatar=avatar: self.run_avatar_yuanying_rift_loop(avatar, initial_delay=0))
            # 所有分身都启动此循环，内含对宗门指令的身份判定。
            self.create_scheduler_task(f"avatar_star_palace_{avatar}", lambda avatar=avatar: self.run_avatar_star_palace_loop(avatar, initial_delay=0))
            if avatar in STAR_ATTRACTION_AVATARS:
                self.create_scheduler_task(f"avatar_star_attraction_{avatar}", lambda avatar=avatar: self.run_avatar_star_attraction_loop(avatar, initial_delay=0))
            if avatar == TAIYI_GUIDE_AVATAR:
                self.create_scheduler_task(f"avatar_taiyi_guide_{avatar}", lambda avatar=avatar: self.run_avatar_taiyi_guide_loop(avatar, initial_delay=0))
            if avatar == CLOUD_STAIRS_AVATAR:
                self.create_scheduler_task(f"avatar_cloud_stairs_{avatar}", lambda avatar=avatar: self.run_avatar_cloud_stairs_loop(avatar, initial_delay=0))
        log.info(f"Avatar loops started for: {', '.join(self.avatars)} (concurrent lock mode)")

        while self.is_running:
            await asyncio.sleep(60)


if __name__ == '__main__':
    """脚本入口：启动 CultivatorXiaoHao"""
    c = CultivatorXiaoHao()
    try:
        asyncio.run(c.start())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal: {e}")
