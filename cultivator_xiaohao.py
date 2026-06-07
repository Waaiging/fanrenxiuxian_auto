#!/usr/bin/env python3
"""
【万灵宗（小号）自动修仙脚本 v8.5】

本脚本是「凡人修仙传」Telegram 游戏的万灵宗角色自动修仙脚本。
负责自动完成万灵宗特有的灵兽玩法循环：
  1. 灵兽管理 —— 缓存灵兽列表，自动放生/寻觅/更新状态
  2. 灵兽探渊 —— 派遣最高战力灵兽探索万兽渊（6h CD）
  3. 灵兽偷菜 —— 派遣灵兽偷取资源（4h CD）
  4. 一键放养 + 灵兽互动/巡游 —— 批量放养、六翼互动与休息状态巡游
  5. 深度闭关 —— 自动开闭关、8小时等待、结算重开
  6. 每日任务 —— 闯塔、宗门点卯
  7. 元婴出窍 —— 元婴期能力循环
  8. 探寻裂缝 —— 定时搜寻裂缝
  9. 抚摸法宝 —— 本命法宝器灵互动
  10. 关键词提醒 —— 监听群聊关键词发送通知

区分于星宫脚本 (sub_cultivator.py)：
- 没有星宫独有的观星/改进星移功能
- 增加了完整的灵兽培养放养探渊偷菜体系
- 使用 beast_lock 而非 cmd_lock 管理灵兽操作
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
from auto_reply_features import is_auto_reply_followup, maybe_auto_reply_exchange
from common_command_features import CommonCommandMixin, common_command_default_state
from command_feedback import send_and_wait_feedback_common
from concubine_features import ConcubineMixin, concubine_default_state
from log_utils import (
    CommandLogFilter, cap_command_retries, command_send_allowed, command_send_precheck, handle_anti_bot_challenge,
    is_deep_meditation_ongoing_response, is_deep_meditation_settlement_response, is_game_bot_sender,
    is_not_deep_meditation_response, log_edited_message_if_needed, log_incoming_message,
    log_manual_outgoing_if_needed, log_mention_if_needed, mentions_self, notify_unrecognized_response,
    match_pending_edited_feedback,
    periodic_log_prune, prune_log_file, record_bot_no_response, record_bot_response,
    record_cultivation_profile_from_text,
    record_game_bot_activity, record_manual_command_reply_state_if_needed,
    recent_profile_identity_for_text,
    remember_script_send_intent, remember_script_sent_message,
    schedule_command_auto_delete, send_text_alert, is_edited_message_for_current_account, wait_for_bot_activity_before_send,
    feedback_response_conflicts,
    feedback_response_matches_command,
    feedback_response_requires_positive_match,
    is_reply_to_manual_command,
    mentions_other_user,
    mentions_other_user_for_identity,
    text_targets_current_account,
)

# ============================================================
# 星宫观星与改换星移全局常量（同步自 sub_cultivator.py 原版逻辑）
# ============================================================
STAR_GAZING_INTERVAL_HOURS = 3                       # 显现间隔 3 小时
STAR_GAZING_MONITOR_LEAD_SECONDS = 3 * 60            # 提前 3 分钟开始监听
STAR_GAZING_COMMAND_LEAD_SECONDS = 30                # 候选整点前 30 秒发送 .观星
STAR_GAZING_SHIFT_LEAD_SECONDS = -25          # 改换星移在显现后 25 秒发出，避开 +38s 最终快报
STAR_SHIFT_TARGET = "TitanCreeper"            # 分身改换星移的目标用户名
STAR_GAZING_ACTIVE_WINDOW_SECONDS = 59               # 即时模式活跃窗口为 59 秒
STAR_GAZING_OPPORTUNITY_START_HOUR = 1               # 凌晨 1:00 后才开始监听好兆头
STAR_GAZING_GOOD_KEYWORDS = ("【Good - 地磁暴动】", "【Good - 星辰异象】", "【Good - 五彩缤纷】", "【Good - 封魔裂隙回响】")
STAR_GAZING_ROTATING_AVATARS = ["素心子", "缘生子"]  # 观星轮换化身列表：每次 Good 事件只派一个化身
STAR_ATTRACTION_TARGET = "天雷星"
STAR_ATTRACTION_COMMAND = f".牵引星辰 {STAR_ATTRACTION_TARGET}"
STAR_ATTRACTION_COOLDOWN_SECONDS = 36 * 3600
STAR_PRE_APPEASE_LEAD_SECONDS = 60
STAR_STATUS_RETRY_SECONDS = 10 * 60
STAR_INSUFFICIENT_RETRY_SECONDS = 60 * 60
STAR_ATTRACTION_AVATARS = {"素心子", "缘生子"}

# =====================================================================
# 路径与常量配置
# =====================================================================
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOG_FILE = os.path.join(CONFIG_DIR, 'cultivator_xiaohao.log')
STATE_FILE = os.path.join(CONFIG_DIR, 'state_xiaohao.json')

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
HUNT_CD_SECONDS = 6 * 3600                     # 寻觅灵兽 CD 6 小时
HUNT_FULL_RETRY_SECONDS = 10 * 60              # 灵兽袋满重试 10 分钟
HUNT_FAIL_RETRY_SECONDS = 60 * 60              # 寻觅失败重试 1 小时
PASTURE_CD_SECONDS = 4 * 3600 + 60             # 历史一键放养被动同步保留
PASTURE_RETURN_DELAY_SECONDS = 60              # 放养归来后延迟
BEAST_FOCUS_NAME = "六翼"
BEAST_INTERACTION_COMMAND = f".灵兽互动 {BEAST_FOCUS_NAME}"
BEAST_SOOTHE_COMMAND = f".灵兽互动 {BEAST_FOCUS_NAME} 安抚"
BEAST_INTERACTION_CD_SECONDS = 90 * 60
BEAST_CRUISE_COMMAND = f".灵兽巡游 {BEAST_FOCUS_NAME}"
BEAST_CRUISE_CD_SECONDS = 120 * 60
BEAST_ACTION_RETRY_SECONDS = 10 * 60
BEAST_ABYSS_MIN_STAMINA = 30
DAILY_TASK_START_HOUR = 7                      # 每日任务开始时间
DAILY_TASK_START_MINUTE = 30
SECT_SKILL_MAX_DAILY = 3                       # 宗门传功每日上限
TREASURE_TOUCH_COMMAND = ".抚摸法宝 青竹蜂云剑"
TREASURE_TOUCH_CD_SECONDS = 2 * 3600
YUANYING_OUT_CD_SECONDS = 8 * 3600
RIFT_SEARCH_CD_SECONDS = 12 * 3600


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

class CultivatorXiaoHao(CommonCommandMixin, ConcubineMixin):
    """
    万灵宗小号脚本主类。
    继承 CommonCommandMixin（通用指令）和 ConcubineMixin（侍妾功能）。
    核心功能围绕灵兽展开：寻觅、放养、探渊、偷菜。
    """

    def __init__(self, session_name='xiaohao_session'):
        """初始化：加载配置、连接 Telegram、初始化状态"""
        self.account_key = "xiaohao"
        self.config = load_config()
        self.mc = self.config.get('monitor', {})
        self.session_file = os.path.join(CONFIG_DIR, session_name)
        self.client = TelegramClient(self.session_file, self.config['api_id'], self.config['api_hash'])
        self.target_chat_id = self.mc.get('chat_id', 1680975844)
        self.topic_id = self.mc.get('topic_id', 7310786)
        self.watch_bot = self.mc.get('watch_bot', 'fanrenxiuxian_bot').lower().lstrip('@')
        self.notify_users = [u.lower() for u in self.mc.get('notify_users', [])]
        self.keywords = [k.lower() for k in self.mc.get('keywords', [])]
        self.sect_name = "万灵宗"
        self.field_training_command = ".野外历练 谨慎"
        self.notified_alert_ids = set()

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
        self.beast_wakeup = asyncio.Event()
        self.startup_done = asyncio.Event()
        self.pause_event = asyncio.Event()  # 暂停/恢复控制（set=运行中, clear=暂停中）
        self.pause_event.set()  # 默认运行中
        # startup: check is_paused to restore paused state
        self.active_atomic_task = None       # 整体任务独占锁持有任务
        self._avatar_loop_count = 0          # 活跃化身循环计数（阻止主循环自动切回主魂）
        # 止/启管理员名单（只有这些人发"止"才生效）
        self.pause_admins = set(self.mc.get("pause_admins", [8325841058, -1003658665113, -1003843934428, -1003996748766]))  # 主魂(TitanCreeper)+问心子+素心子+缘生子
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
        # startup: restore paused state
        if self.state.get("is_paused", False):
            self.pause_event.clear()
            log.info("Startup: is_paused=True, entering paused state.")
        self._current_identity = self.state.get("current_identity", "主魂")
        self._main_confirmed = (self._current_identity == "主魂")  # 启动时若上次为主魂则默认确认，否则强制对齐
        self._switch_lock = asyncio.Lock()  # 防止多个任务同时发送 .切换 主魂
        self.ensure_avatar_states()
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
        elif "3996748766" in sender_id: return "缘生子"
        elif "3843934428" in sender_id: return "素心子"
        elif "3658665113" in sender_id: return "问心子"
        return None

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
            "deep_meditation_end_time": "", "in_deep_meditation": False,
            "concubine_recalled_for_meditation": False, "concubine_recalled_time": "",
            "next_hunt_time": "", "next_steal_time": "", "next_abyss_time": "",
            "next_pasture_time": "", "pasture_pending_count": 0,
            "pasture_returned_count": 0, "pasture_pending_since": "",
            "last_pasture_return_time": "", "next_meditation_retry_time": "",
            "best_beast_injured_time": "", "next_beast_status_check_time": "",
            "best_beast_name": "", "best_beast_power": 0,
            "best_beast_status": "", "best_beast_stamina": -1, "best_beast_injury_source": "",
            "beast_hunt_stopped": False, "beast_hunt_stopped_reason": "",
            "beasts_cache": [],
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
            "next_meditation_retry_time": "",
            "next_field_training_time": "",
            "last_field_training_time": "",
            "nickname": "",
            "last_tower_date": "",
            "level": "",
            "next_star_attraction_time": "",
            "last_star_attraction_time": "",
            "next_star_appease_time": "",
            "last_star_appease_time": "",
            "next_star_collect_time": "",
            "last_star_collect_time": "",
            "next_star_check_time": "",
            "last_star_observatory_time": "",
            "star_observatory_summary": "",
            "star_observatory_needs_refresh": True,
            "star_attraction_retry_time": "",
            "star_attraction_force_exit_tried": False,
            "star_target": STAR_ATTRACTION_TARGET,
            "next_dream_map_time": "",
            "next_heart_trial_time": "",
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

    def clear_stale_meditation_state(self):
        """清理已经过期的本地闭关标记，启动后再由 .查看闭关 对账。"""
        changed = False

        def normalize(container):
            nonlocal changed
            end_time = container.get("deep_meditation_end_time", "")
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
        self.ensure_avatar_states()
        return self.state["avatars"].get(avatar, {})

    def set_avatar_state(self, avatar, key, value):
        """设置指定分身的状态并保存"""
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar][key] = value
            self.save_state()

    def update_avatar_states(self, avatar, values):
        """批量更新分身状态，避免连续写入 state 文件。"""
        self.ensure_avatar_states()
        if avatar in self.state["avatars"]:
            self.state["avatars"][avatar].update(values)
            self.save_state()

    # ---- 身外化身：被动身份更新 ----

    def update_identity_passively(self, msg):
        """
        从 bot 回复中被动探测当前身份。
        当检测到切换成功的回复时，自动修正 self.current_identity。
        """
        text = msg.text or ""
        if not text:
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
        
        # 严格过滤：如果消息有明确的接收人但不是我，一律无视（防止同群串号）
        recent_identity = recent_profile_identity_for_text(self, text, msg_id=getattr(msg, "id", None))
        avatar_marker = next((name for name in self.avatars if f"[Avatar: {name}]" in text), None)
        if not self.text_targets_self(msg, text) and not recent_identity and not avatar_marker:
            return
        
        avatar = recent_identity or avatar_marker or None
        attribution_reliable = bool(recent_identity or avatar_marker)  # 身份归属是否可靠（sender_id/reply_to/text特征命中）
        # 优先根据群组/频道 ID 强制判断发送者（防串号）
        chat_id = str(msg.chat_id)
        sender_id = str(getattr(msg, "sender_id", ""))
        target_str = str(self.target_chat_id).replace("-100", "")
        if not avatar and target_str in sender_id:
            avatar = "主魂"
            attribution_reliable = True
        elif not avatar and "3996748766" in sender_id:
            avatar = "缘生子"; attribution_reliable = True
        elif not avatar and "3843934428" in sender_id:
            avatar = "素心子"; attribution_reliable = True
        elif not avatar and "3658665113" in sender_id:
            avatar = "问心子"; attribution_reliable = True
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
            
        if avatar == "主魂":
            self.save_state()

        if not avatar or avatar == "主魂": return  # 主魂由原有框架处理
        
        now = now_str()

        # 1. 强行出关 / 出关成功 → 清除深度闭关状态
        # ⚠️ "功成圆满" 会出现在 ".查看闭关" 正常回复中（"即可功成圆满"），必须排除"预计还需"的情况
        _is_real_exit = any(k in text for k in ["强行出关", "出关成功", "已出关", "闭关结束"]) or ("功成圆满" in text and "预计还需" not in text and "正在" not in text)
        if _is_real_exit:
            self.set_avatar_state(avatar, "in_deep_meditation", False)
            self.set_avatar_state(avatar, "deep_meditation_end_time", "")
            log.info(f"Avatar {avatar}: passive detect closing state cleared (出关).")

        # 2. 深度闭关 / 预计还需 → 更新深度闭关结束时间
        elif any(k in text for k in ["深度闭关", "开始闭关", "进入闭关", "开启闭关", "查看闭关", "预计还需"]):
            # 如果是明确的未闭关/出关/结算文案，清除闭关状态
            is_ongoing = any(k in text for k in ["预计还需", "还需"])
            if any(k in text for k in ["未处于深度闭关", "并未处于深度闭关", "结算", "归位"]) or ("功成圆满" in text and not is_ongoing):
                self.set_avatar_state(avatar, "in_deep_meditation", False)
                self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                log.info(f"Avatar {avatar}: passive detect Meditation end/idle.")
            else:
                cd = self.parse_wait_time(text)
                if cd > 0:
                    self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now, cd))
                    self.set_avatar_state(avatar, "in_deep_meditation", True)
                    log.info(f"Avatar {avatar}: passive detect Meditation active. End in {cd}s.")

        # 3. 闭关冷却中 → 更新 next_meditation_time
        if "闭关冷却" in text or "闭关剩余" in text:
            cd = self.parse_wait_time(text, line_identifier="闭关")
            if cd > 0:
                self.set_avatar_state(avatar, "next_meditation_time", add_seconds_str(now, cd))
                log.info(f"Avatar {avatar}: passive meditation cooldown {cd}s.")

        # 4. 闯塔
        if "通关" in text and ("层" in text or "塔" in text):
            today = datetime.now().strftime("%Y-%m-%d")
            self.set_avatar_state(avatar, "last_tower_date", today)
            log.info(f"Avatar {avatar}: passive tower cleared today.")
        elif "闯塔冷却" in text or "今日已闯" in text:
            today = datetime.now().strftime("%Y-%m-%d")
            self.set_avatar_state(avatar, "last_tower_date", today)

        # 6. 入梦寻图
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

    def record_avatar_cloud_stairs_response(self, avatar, stairs_resp, source="Cloud stairs climb"):
        if not stairs_resp:
            return False

        # ---- 登阶成功 ----
        if "【凌霄云阶】" in stairs_resp or ("踏上" in stairs_resp and "云阶" in stairs_resp):
            self._avatar_update_cloud_stairs_progress_from_text(avatar, stairs_resp, source=source)
            self.update_completed_weeks_from_text(stairs_resp, source=source)

            now = now_str()
            self.set_avatar_state(avatar, "last_stairs_time", now)
            self.set_avatar_state(avatar, "last_stairs_success_time", now)
            self.set_avatar_state(avatar, "next_stairs_time", add_seconds_str(now, 3 * 3600))
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
                return
            # 冷却中，跳过
            if self.is_avatar_heart_platform_throttled(avatar):
                return
            # 今天已使用，跳过
            if self.avatar_heart_platform_already_used_today(avatar, today):
                return
    
            # 罡风 buff 还在的话，问心台让路
            wind_pending = self.avatar_has_pending_wind_buff(avatar)
            if wind_pending:
                log.info(f"Heart Platform [{avatar}] skipped: pending Wind buff has priority.")
                return
    
            # 如果罡风冷却完毕但还没用，优先用罡风
            if self.is_avatar_nine_heaven_wind_ready(avatar):
                log.info(f"Heart Platform check at {curr_step}/12 for [{avatar}], but Wind is ready. Sending .引九天罡风 first; Heart Platform is skipped.")
                wind_resp = await self.send_and_wait_feedback_identity(avatar, ".引九天罡风", timeout=120, force_identity_check=True)
                wind_pending = self.record_avatar_nine_heaven_wind_response(avatar, wind_resp, source="Cloud Stairs")
                await asyncio.sleep(3)
    
                # 如果罡风用了或者还是冷却完毕状态（说明施展失败），跳过问心台
                if wind_pending or self.is_avatar_nine_heaven_wind_ready(avatar):
                    log.info(f"Heart Platform [{avatar}] skipped to preserve Wind priority.")
                    return
    
            # 使用问心台
            reason = "late daily fallback" if late_fallback and not (8 <= curr_step <= 11) else "late cloud-stairs climb"
            log.info(f"Progress {curr_step}/12 for [{avatar}], Wind unavailable, sending .问心台 for {reason}.")
            self.set_avatar_state(avatar, "heart_platform_date", today)
            self.set_avatar_state(avatar, "last_heart_time", now_str())
            self.set_avatar_state(avatar, "next_heart_time", add_seconds_str(f"{today} 00:05:00", 24 * 3600))
            self.save_state()
    
            hp_resp = await self.send_and_wait_feedback_identity(avatar, ".问心台", force_identity_check=True)
            if hp_resp:
                if any(k in hp_resp for k in ["问心台", "已经", "明天", "成功", "感受到", "感悟", "今日"]):
                    log.info(f"Heart Platform [{avatar}] used/confirmed for late cloud-stairs climb.")
                else:
                    log.warning(f"Heart Platform [{avatar}] response unusual: {hp_resp[:100]}")
                    notify_unrecognized_response(self, ".问心台", hp_resp, log, "问心台")
    
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
    
                variance = random.randint(10, 30)
                log.info(f"Cloud Stairs Loop Complete. Sleep {wait_time + variance}s.")
                await asyncio.sleep(wait_time + variance)

    # ---- 身外化身：物理串行发送管线 ----

    def _state_impending_command_wait(self, state, identity=""):
        """获取身份距离下一个待执行指令的等待时间（秒）。如果没有，返回 999999。"""
        if not isinstance(state, dict):
            return 999999
        now_dt = datetime.now()
        min_wait = 999999
        
        keys_to_check = [
            "next_meditation_retry_time",
            "next_field_training_time",
            "next_tower_time",
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
            "next_heart_trial_time",
            "next_divination_time",
            "next_stairs_time",
            "next_heart_time",
            "next_heart_platform_time",
            "next_steal_time",
            "next_beast_status_check_time",
            "next_pasture_time",
            "next_beast_interaction_time",
            "next_beast_cruise_time",
            "next_treasure_touch_time",
            "next_yuanying_out_time",
            "next_rift_search_time",
            "next_switch_allowed_time",
        ]
        
        for k in keys_to_check:
            if k == "next_switch_allowed_time":
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
            done = set(state.get("done", [])) if isinstance(state.get("done"), list) else set()
            daily_due = (
                (".宗门点卯" not in done and not self.dashboard_command_paused(".宗门点卯", identity))
                or (".闯塔" not in done and not self.dashboard_command_paused(".闯塔", identity))
            )
            if seconds_until_daily_task_start(datetime.now()) <= 0 and daily_due:
                min_wait = min(min_wait, 0)

        if (
            identity in self.avatars
            and state.get("last_dianmao_date") != datetime.now().strftime("%Y-%m-%d")
            and not self.dashboard_command_paused(".宗门点卯", identity)
            and seconds_until_daily_task_start(datetime.now()) <= 0
        ):
            min_wait = min(min_wait, 0)

        if not state.get("in_deep_meditation") and not state.get("deep_meditation_end_time") and not state.get("next_meditation_retry_time"):
            min_wait = 0
            
        return min_wait

    def get_identity_impending_command_wait(self, identity):
        if identity == "主魂":
            wait = self._state_impending_command_wait(self.state, identity="主魂")
            return self.merge_impending_wait(wait, self.custom_command_impending_wait("主魂"))
        if identity in self.avatars:
            wait = self._state_impending_command_wait(self.get_avatar_state(identity), identity=identity)
            return self.merge_impending_wait(wait, self.custom_command_impending_wait(identity))
        return 999999

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

    async def send_and_wait_feedback_identity(self, identity, message, timeout=45, max_retries=2, force_identity_check=False, **kwargs):
        """
        带身份感知的物理串行发送管线。
        """
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停守卫：等待恢复信号
        await self.pause_event.wait()

        _t0 = time.monotonic()
        log.info(f"[DEBUG-IDENTITY] [{identity}] ENTER send_and_wait_feedback_identity, cmd={message!r}, lock_held={self.avatar_send_lock.locked()}")
        yield_attempts = 0
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            lock_wait_start = time.monotonic()
            async with self.avatar_send_lock:
                _lock_wait = time.monotonic() - lock_wait_start
                if _lock_wait > 5:
                    log.warning(f"[DEBUG-IDENTITY] [{identity}] avatar_send_lock acquired after {_lock_wait:.1f}s (long wait!)")
                else:
                    log.info(f"[DEBUG-IDENTITY] [{identity}] avatar_send_lock acquired in {_lock_wait:.1f}s")

                if self.active_atomic_task is not None and self.active_atomic_task != current_t:
                    should_yield = True
                    wait_sec_to_sleep = 0.5
                elif self.current_identity and self.current_identity != identity:
                    wait_sec = self.get_identity_impending_command_wait(self.current_identity)
                    if 0 <= wait_sec <= 60:
                        if yield_attempts == 0 or yield_attempts % 12 == 0:
                            log.info(f"Avatar switch deferred: {self.current_identity} has commands due in {wait_sec:.1f}s. [{identity}] yields lock.")
                        yield_attempts += 1
                        should_yield = True
                        wait_sec_to_sleep = max(5, min(wait_sec + 2, 30))
                
                if not should_yield:
                    if force_identity_check or self.current_identity != identity:
                        ban_time = self.state.get("next_switch_allowed_time", "")
                        if ban_time and is_future(ban_time):
                            log.warning(f"🚫 Global switch is banned until {ban_time}. Blocking switch to {identity}.")
                            return None
                        switch_target = "主魂" if identity == "主魂" else identity
                        switch_cmd = f".切换 {switch_target}"
                        log.info(f"🔄 Avatar switch: {self.current_identity} → {identity} (sending {switch_cmd})")
                        log.info(f"[DEBUG-IDENTITY] [{identity}] sending switch cmd: {switch_cmd}")
                        switch_resp = await self._send_and_wait_feedback_raw(switch_cmd, timeout=30, max_retries=2)
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        log.info(f"[DEBUG-IDENTITY] [{identity}] switch response: {resp_str[:120]!r}")
                        if not resp_str and self.apply_switch_guard_backoff(switch_cmd):
                            log.error(f"❌ Switch to {identity} blocked by command guard. Blocking subsequent command: {message}")
                            return None
                        if self.check_and_record_switch_ban(resp_str):
                            log.error(f"❌ Switch to {identity} failed due to global command ban. Blocking subsequent command: {message}")
                            return None
                        is_success = False
                        if resp_str and any(k in resp_str for k in ["成功", "已切换", "当前操控", identity]):
                            is_success = True
                        if is_success:
                            self.current_identity = identity
                            self._main_confirmed = False  # 化身已切换，主魂确认失效
                            log.info(f"✅ Avatar switch confirmed: now {identity}")
                        else:
                            log.error(f"❌ Avatar switch to {identity} FAILED! Blocking subsequent command: {message}. Response: {resp_str[:120]}")
                            return None
                        await asyncio.sleep(2)
                    else:
                        log.info(f"[DEBUG-IDENTITY] [{identity}] already in correct identity, skip switch")

                    log.info(f"[DEBUG-IDENTITY] [{identity}] sending cmd: {message!r}")
                    resp = await self._send_and_wait_feedback_raw(message, timeout=timeout, max_retries=max_retries, **kwargs)
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
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
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
                async with self.avatar_send_lock:
                    if self.active_atomic_task is not None and self.active_atomic_task != current_t:
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
                            resp = await self._send_and_wait_feedback_raw(".切换 主魂", timeout=10, max_retries=0)
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

    async def send_to_game(self, message, reply_to=None):
        """发送指令到游戏群组（带活跃度检测和守卫）"""
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停阻断守卫
        await self.pause_event.wait()

        try:
            target_reply = reply_to.id if hasattr(reply_to, "id") else (reply_to if reply_to else self.topic_id)
            if not await wait_for_bot_activity_before_send(self, message, log):
                return None
            if not command_send_allowed(self, message, log):
                return None
            remember_script_send_intent(self, message)
            msg = await self.client.send_message(self.target_chat_id, message, reply_to=target_reply)
            remember_script_sent_message(self, msg)
            # 记录 msg_id → avatar，供回复归属判断
            self.command_avatar_map[msg.id] = self.current_identity
            schedule_command_auto_delete(self, msg, text=message, logger=log)
            log.info(f"🟢 OUT [{self.current_identity}]:\n{message}")
            return msg.id
        except Exception as e:
            log.error(f"Send Error [{message}] reply_to={target_reply}: {e}")
            return None

    async def send_and_wait_feedback(self, message, timeout=45, max_retries=2, reply_to=None, return_msg=False, return_response_msg=False, delete_after=True, force_identity_check=False):
        """
        发送指令并等待回复（带 avatar_send_lock 保护）。
        所有主魂业务通过此方法发送。如果当前身份不是主魂，自动切回主魂再发送。
        """
        # 整体任务独占锁守卫
        current_t = asyncio.current_task()
        while self.active_atomic_task is not None and self.active_atomic_task != current_t:
            await asyncio.sleep(0.5)

        # 暂停守卫：等待恢复信号
        await self.pause_event.wait()

        yield_attempts = 0
        while True:
            should_yield = False
            wait_sec_to_sleep = 0
            async with self.avatar_send_lock:
                # 主魂自动身份对齐：如果当前是分身身份，或者主魂未确认，先切回主魂
                # avatar_send_lock 已防止并发冲突，化身下次 send_and_wait_feedback_identity 会自行切回
                if self.active_atomic_task is not None and self.active_atomic_task != current_t:
                    should_yield = True
                    wait_sec_to_sleep = 0.5
                elif force_identity_check or self.current_identity != "主魂" or not self._main_confirmed:
                    if not command_send_precheck(self, message, log, identity="主魂"):
                        log.info(f"Skip auto-switch to 主魂: main command is not sendable now ({message}).")
                        return None
                    if not force_identity_check and self.current_identity in self.avatars:
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
                        # 检查全局切换封禁是否在冷却中
                        ban_time = self.state.get("next_switch_allowed_time", "")
                        if ban_time and is_future(ban_time):
                            log.warning(f"🚫 Global switch is banned until {ban_time}. Blocking auto-switch back to 主魂.")
                            return None

                        log.info(f"🔄 Auto switch back to 主魂 from {self.current_identity} (before main command)")
                        switch_resp = await self._send_and_wait_feedback_raw(".切换 主魂", timeout=30, max_retries=2)
                        resp_str = getattr(switch_resp, "text", "") if hasattr(switch_resp, "text") else switch_resp if isinstance(switch_resp, str) else ""
                        
                        if not resp_str and self.apply_switch_guard_backoff(".切换 主魂"):
                            log.error(f"❌ Auto-switch back to 主魂 blocked by command guard. Blocking main command: {message}")
                            return None

                        # 检测是否被封禁
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
                    # 主魂境界由 maybe_record_avatar_passive_states 统一更新（有归属校验，防污染）
                    return resp

            if should_yield:
                await asyncio.sleep(wait_sec_to_sleep)

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

    # ---- 每日任务 ----

    async def run_daily_tasks(self):
        """
        每日任务循环：
        1. 闯塔
        2. 宗门点卯
        """
        await self.startup_done.wait()
        while self.is_running:
            now = datetime.now()
            daily_wait = seconds_until_daily_task_start(now)
            if daily_wait > 0:
                next_run = now + timedelta(seconds=daily_wait)
                log.info(f"Daily tasks paused before {daily_task_start_label()}. Next check at {dt_to_str(next_run)}.")
                await asyncio.sleep(daily_wait + random.randint(0, 30))
                continue

            today = now.strftime('%Y-%m-%d')
            if self.state.get("date") != today:
                log.info(f"New Day Detected ({today}): Resetting state.")
                self.state["date"] = today
                self.state["done"] = []
                self.state["sect_skill_count"] = 0
                self.save_state()

            # 1. 每日任务
            dianmao_msg_id = None
            for t in [".闯塔", ".宗门点卯"]:
                if t not in self.state["done"]:
                    sent_msg = await self.send_and_wait_feedback(t, return_msg=True)
                    if sent_msg:
                        self.state["done"].append(t)
                        if t == ".宗门点卯":
                            self.state["last_dianmao_msg_id"] = sent_msg.id
                        self.save_state()
                    await asyncio.sleep(5)

            await asyncio.sleep(600)

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
        if not resp:
            return "unknown"
        count_match = re.search(r'今日已传功\s*\**\s*(\d+)\s*/\s*3', resp)
        if count_match:
            self.state["sect_skill_count"] = max(self.state.get("sect_skill_count", 0), int(count_match.group(1)))
            return "counted"
        if any(k in resp for k in ["次数不足", "明日再来", "已经", "过于频繁"]):
            self.state["sect_skill_count"] = SECT_SKILL_MAX_DAILY
            return "done"
        if any(k in resp for k in ["失败", "需回复", "主魂"]):
            log.warning(f"Sect skill reply target invalid: {resp[:80]}...")
            return "invalid"
        if any(k in resp for k in ["成功", "元神", "传功", "玉简"]):
            self.state["sect_skill_count"] = min(SECT_SKILL_MAX_DAILY, self.state.get("sect_skill_count", 0) + 1)
            return "counted"
        notify_unrecognized_response(self, ".宗门传功", resp, log, "宗门传功")
        return "unknown"

    # ---- 抚摸法宝 ----

    def record_treasure_touch_response(self, resp):
        """解析抚摸法宝回复，记录冷却"""
        command = TREASURE_TOUCH_COMMAND
        next_key = "next_treasure_touch_time"
        last_key = "last_treasure_touch_time"
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需", "互动"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            return False
        if any(k in resp for k in ["联系更加紧密", "器灵传来了喜悦", "默契", "经验", "与它互动", "微微颤动"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, TREASURE_TOUCH_CD_SECONDS)
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
        self.state[next_key] = add_seconds_str(now_str(), 600)
        notify_unrecognized_response(self, command, resp, log, "抚摸法宝")
        return False

    async def _wait_for_main_identity(self):
        """
        主循环身份守卫：只等待正在发送的化身指令完成。
        不在这里主动切回主魂；真正要发送主魂指令时由 send_and_wait_feedback 对齐身份。
        """
        while self.avatar_send_lock.locked():
            await asyncio.sleep(1)

    async def run_treasure_touch_loop(self):
        """抚摸法宝循环：到冷却发指令"""
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            next_time = self.state.get("next_treasure_touch_time", "")
            if next_time and is_future(next_time):
                await asyncio.sleep(min(seconds_until(next_time), 600))
                continue
            log.info(f"Treasure touch due: sending {TREASURE_TOUCH_COMMAND}.")
            resp = await self.send_and_wait_feedback(
                TREASURE_TOUCH_COMMAND,
                timeout=90,
                force_identity_check=True,
            )
            self.record_treasure_touch_response(resp)
            self.save_state()
            await asyncio.sleep(min(seconds_until(self.state.get("next_treasure_touch_time", "")) or 600, 600))

    # ---- 元婴出窍 / 探寻裂缝 ----

    def record_fixed_cd_command_response(self, resp, command, last_key, next_key, cd_seconds):
        """通用固定冷却指令回复处理"""
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            self.save_state()
            return False
        if any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False
        now = now_str()
        success_keywords = ["成功", "探寻", "裂缝", "收获", "空间", "发现"]
        if not any(k in resp for k in success_keywords):
            self.state[next_key] = add_seconds_str(now, 600)
            notify_unrecognized_response(self, command, resp, log, "固定冷却指令")
            self.save_state()
            return False
        self.state[last_key] = now
        self.state[next_key] = add_seconds_str(now, cd_seconds)
        self.save_state()
        return True

    def record_yuanying_out_start_response(self, resp):
        """解析元婴出窍回复"""
        if not resp:
            self.state["next_yuanying_out_time"] = add_seconds_str(now_str(), 600)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now_str(), cd)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            return False
        now = now_str()
        if any(k in resp for k in ["尚未凝聚元婴", "无法施展此术"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now, 6 * 3600)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            log.info(f".元婴出窍 unavailable: next check at {self.state['next_yuanying_out_time']}.")
            return False
        if not any(k in resp for k in ["元婴出窍", "神游", "云游", "出窍", "自动结算"]):
            self.state["next_yuanying_out_time"] = add_seconds_str(now, 600)
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            notify_unrecognized_response(self, ".元婴出窍", resp, log, "元婴出窍")
            return False
        cd = cd if cd > 0 else YUANYING_OUT_CD_SECONDS
        self.state["last_yuanying_out_time"] = now
        self.state["next_yuanying_out_time"] = add_seconds_str(now, cd)
        self.state["yuanying_out_end_time"] = self.state["next_yuanying_out_time"]
        self.state["yuanying_out_active"] = True
        return True

    def is_rift_weakness_response(self, text):
        """检测"元婴虚弱期"回放（需要紧急停止脚本）"""
        if not text:
            return False
        clean = text.replace("**", "").replace(" ", "")
        return (
            "元婴遁逃·虚弱" in clean
            or ("虚弱期" in clean and "无法进行夺舍" in clean)
            or ("神魂遭受重创" in clean and "虚弱" in clean)
        )

    async def stop_for_rift_weakness(self, response):
        """检测到元婴虚弱期：发送告警并停止脚本"""
        self.state["next_rift_search_time"] = ""
        self.save_state()
        await send_text_alert(
            self, "万灵宗探寻裂缝告警",
            "探寻裂缝触发元婴虚弱期，脚本已停止，请手动处理。\n\n" f"机器人回复：\n{response}",
            log,
        )
        log.critical(f"Rift weakness detected. Stopping xiaohao script:\n{response}")
        self.is_running = False

    async def run_yuanying_out_loop(self):
        """元婴出窍循环：到点自动归窍，再重新出窍"""
        await self.startup_done.wait()
        while self.is_running:
            await self._wait_for_main_identity()
            # 境界自适应校验
            main_level = self.state.get("level", "")
            if not main_level:
                log.info("Main level not cached, sending .状态 to fetch it...")
                await self.send_and_wait_feedback(".状态", timeout=30)
                main_level = self.state.get("level", "")

            end_time = self.state.get("yuanying_out_end_time") or self.state.get("next_yuanying_out_time", "")
            active = self.state.get("yuanying_out_active")
            # 已出窍 → 必须先自动归窍，不检查境界
            if active and end_time and is_future(end_time):
                log.info(f"Yuanying out active. Auto-return due at {end_time}.")
                await asyncio.sleep(min(seconds_until(end_time), 600))
                continue
            if active:
                log.info("Yuanying out time expired. Auto-resetting state.")
                # 元婴自动归窍，直接重置状态
                self.state["yuanying_out_active"] = False
                self.state["yuanying_out_end_time"] = ""
                self.save_state()
                await asyncio.sleep(5)
            # 境界检查：只在开启新出窍时检查，自动归窍不检查
            has_yuanying = any(k in main_level for k in ["元婴", "化神", "合体", "大乘", "渡劫", "仙"])
            if not has_yuanying:
                log.info(f"🚫 Main soul level [{main_level}] has no Yuanying. Yuanying out loop suspended for 1 hour.")
                await asyncio.sleep(3600)
                continue

            next_time = self.state.get("next_yuanying_out_time", "")
            if next_time and is_future(next_time):
                await asyncio.sleep(min(seconds_until(next_time), 600))
                continue
            log.info("Yuanying ability due: sending .元婴出窍.")
            resp = await self.send_and_wait_feedback(".元婴出窍", timeout=120)
            self.record_yuanying_out_start_response(resp)
            self.save_state()
            await asyncio.sleep(5)

    async def run_rift_search_loop(self):
        """探寻裂缝循环：定时发送.探寻裂缝"""
        await self.startup_done.wait()
        command = ".探寻裂缝"
        last_key = "last_rift_search_time"
        next_key = "next_rift_search_time"
        while self.is_running:
            await self._wait_for_main_identity()
            # 境界自适应校验
            main_level = self.state.get("level", "")
            if not main_level:
                log.info("Main level not cached, sending .状态 to fetch it...")
                await self.send_and_wait_feedback(".状态", timeout=30)
                main_level = self.state.get("level", "")

            has_yuanying = any(k in main_level for k in ["元婴", "化神", "合体", "大乘", "渡劫", "仙"])
            if not has_yuanying:
                log.info(f"🚫 Main soul level [{main_level}] has no Yuanying. Rift search loop suspended for 1 hour.")
                await asyncio.sleep(3600)
                continue

            next_time = self.state.get(next_key, "")
            if next_time and is_future(next_time):
                await asyncio.sleep(min(seconds_until(next_time), 600))
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
            if self.is_rift_weakness_response(resp_text):
                replied_id = getattr(getattr(resp_msg, 'reply_to', None), 'reply_to_msg_id', None) or getattr(resp_msg, 'reply_to_msg_id', None)
                if replied_id and replied_id != getattr(self, 'last_sent_id', None):
                    log.warning(f"Rift weakness detected but reply_to #{replied_id} != our sent msg, likely someone else's. Skipping.")
                    continue
                await self.stop_for_rift_weakness(resp_text)
                break
            self.record_fixed_cd_command_response(resp_text, command, last_key, next_key, RIFT_SEARCH_CD_SECONDS)
            await asyncio.sleep(5)

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
        """检测是否为伪受伤状态（需要切换清除的假状态）"""
        if not text: return False
        clean = text.replace("**", "")
        return ("正在养伤" in clean and "无法出动" in clean) or self.is_abyss_busy_response(clean) or ("当前并非出战状态" in clean and any(k in clean for k in ["受伤", "养伤"]))

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
        beasts = list(cache if cache is not None else self.state.get("beasts_cache", []))
        beasts.sort(key=lambda x: (x.get('power', 0), x.get('exp', 0), x.get('full_name', '')), reverse=True)
        return beasts

    def beast_stamina_value(self, beast):
        try:
            return int(beast.get("stamina", -1))
        except Exception:
            return -1

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

    def set_best_beast_status(self, name, status):
        """设置最佳灵兽的状态并保存"""
        if not name or not status: return
        old_status = ""
        if self.state.get("best_beast_name") == name: old_status = self.state.get("best_beast_status", "")
        for beast in self.state.get("beasts_cache", []):
            if beast.get("full_name") == name: old_status = beast.get("status", "") or old_status; break
        self.adjust_pasture_pending_for_status_change(name, old_status, status)
        if self.state.get("best_beast_name") == name:
            self.state["best_beast_status"] = status
            self.record_best_beast_status_timing(status)
        for beast in self.state.get("beasts_cache", []):
            if beast.get("full_name") == name: beast["status"] = status; break
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
        """六翼放养中时，暂停会撞状态的灵兽指令直到放养冷却结束。"""
        target_time = self.pasture_block_until()
        beast_name = beast_name or BEAST_FOCUS_NAME
        self.set_best_beast_status(beast_name, "放养中")
        self.set_next_abyss_not_before(target_time)
        self.set_next_steal_not_before(target_time)
        for key in (
            "next_beast_interaction_time",
            "next_beast_cruise_time",
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
                self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 600)
                check_time = self.state["next_beast_status_check_time"]
            elif not is_future(check_time):
                log.info("Beast injury status has no remaining cooldown; treating as stale.")
            if check_time and is_future(check_time): self.set_next_abyss_not_before(check_time)
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
        return "出战" in (status or "") or self.is_stale_injury_status(status)

    def can_attempt_abyss_status(self, status):
        """判断灵兽状态是否允许探渊"""
        status = status or ""
        if self.is_pastured_status(status): return False
        if self.is_injury_status(status): return not self.is_injury_recovery_pending(status)
        return not any(k in status for k in ["受伤", "重伤", "治疗", "探险", "偷菜", "巡游"])

    def can_attempt_steal_status(self, status):
        """判断灵兽状态是否允许偷菜"""
        status = status or ""
        if self.is_pastured_status(status): return False
        if any(k in status for k in ["探险", "偷菜", "巡游"]): return False
        if self.is_injury_status(status): return not self.is_injury_recovery_pending(status)
        return True

    # ---- 灵兽：互动/巡游 ----

    def get_cached_beast_by_name(self, target_name=BEAST_FOCUS_NAME):
        """从缓存中查找指定灵兽，支持忽略括号后缀匹配。"""
        for beast in self.state.get("beasts_cache", []):
            if self.beast_name_matches(beast.get("full_name", ""), target_name):
                return beast
        return None

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

    def record_beast_cruise_response(self, resp):
        """解析 .灵兽巡游 六翼 回复并记录 120 分钟冷却。"""
        command = BEAST_CRUISE_COMMAND
        last_key = "last_beast_cruise_time"
        next_key = "next_beast_cruise_time"
        if not resp:
            self.schedule_beast_action_retry(next_key)
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚需", "还需", "请在"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            return True
        if self.is_beast_cruise_blocked_by_deployed(resp):
            self.set_best_beast_status(BEAST_FOCUS_NAME, "出战中")
            self.schedule_beast_action_retry(next_key, 60)
            return True
        if self.is_beast_pastured_response(resp, BEAST_FOCUS_NAME):
            self.defer_beast_actions_while_pastured(BEAST_FOCUS_NAME, "cruise response")
            return True
        if any(k in resp for k in ["需要休息", "休息状态", "无法巡游", "受伤", "重伤", "治疗"]) or (
            "正在" in resp and "正在巡游" not in resp
        ):
            retry = cd if cd > 0 else BEAST_ACTION_RETRY_SECONDS
            self.schedule_beast_action_retry(next_key, retry)
            return True
        if any(k in resp for k in ["巡游", "出发", "游历", "带回", "获得", "收获", "成功"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, BEAST_CRUISE_CD_SECONDS)
            return True
        self.schedule_beast_action_retry(next_key)
        notify_unrecognized_response(self, command, resp, log, "灵兽巡游")
        return False

    def is_beast_cruise_blocked_by_deployed(self, text):
        """检测巡游被出战状态阻塞的明确回复。"""
        text = text or ""
        return BEAST_FOCUS_NAME in text and "无法巡游" in text and "出战" in text

    async def run_focus_beast_cruise(self):
        """发送六翼巡游；若提示出战中无法巡游，先召回休息再重试一次。"""
        resp = await self.send_and_wait_feedback(BEAST_CRUISE_COMMAND, timeout=60, max_retries=1)
        if not self.is_beast_cruise_blocked_by_deployed(resp):
            self.record_beast_cruise_response(resp)
            return

        log.info(f"Beast cruise: {BEAST_FOCUS_NAME} is deployed; resting before retry.")
        self.set_best_beast_status(BEAST_FOCUS_NAME, "出战中")
        rest_status, rest_resp = await self.rest_beast_for_abyss(BEAST_FOCUS_NAME)
        if rest_status and "休息" in rest_status:
            await asyncio.sleep(3)
            retry_resp = await self.send_and_wait_feedback(BEAST_CRUISE_COMMAND, timeout=60, max_retries=1)
            self.record_beast_cruise_response(retry_resp)
            return

        injury_cd = self.record_beast_injury_from_response(BEAST_FOCUS_NAME, rest_resp, source="cruise")
        retry = injury_cd if injury_cd > 0 else BEAST_ACTION_RETRY_SECONDS
        self.schedule_beast_action_retry("next_beast_cruise_time", retry)
        if rest_resp and injury_cd < 0 and not self.is_fake_beast_status_response(rest_resp):
            notify_unrecognized_response(self, f".灵兽休息 {BEAST_FOCUS_NAME}", rest_resp, log, "巡游失败后休息")

    async def prepare_focus_beast_for_cruise(self):
        """确保六翼为休息状态；仅出战中会主动召回，其他忙碌/受伤状态延后。"""
        beast = self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
        if not beast or beast.get("status", "未知") == "未知":
            log.info(f"Beast cruise: {BEAST_FOCUS_NAME} cache/status unknown; retry after next abyss refresh.")
            self.schedule_beast_action_retry("next_beast_cruise_time", BEAST_ACTION_RETRY_SECONDS)
            return False
        if not beast:
            log.warning(f"Beast cruise: {BEAST_FOCUS_NAME} not found in cache; retry later.")
            self.schedule_beast_action_retry("next_beast_cruise_time")
            return False
        beast_name = beast.get("full_name") or BEAST_FOCUS_NAME
        status = beast.get("status", "未知")
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
            retry = injury_cd if injury_cd > 0 else BEAST_ACTION_RETRY_SECONDS
            self.schedule_beast_action_retry("next_beast_cruise_time", retry)
            if rest_resp and injury_cd < 0 and not self.is_fake_beast_status_response(rest_resp):
                notify_unrecognized_response(self, f".灵兽休息 {beast_name}", rest_resp, log, "巡游前休息")
            return False
        retry_time = self.state.get("next_beast_status_check_time", "")
        retry_seconds = int(seconds_until(retry_time)) if retry_time and is_future(retry_time) else BEAST_ACTION_RETRY_SECONDS
        log.info(f"Beast cruise deferred: {beast_name} status is {status}, retry in {retry_seconds}s.")
        self.schedule_beast_action_retry("next_beast_cruise_time", max(60, retry_seconds))
        return False

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
        changed = 0
        for beast in self.state.get("beasts_cache", []):
            if names and not any(self.beast_name_matches(beast.get("full_name"), name) for name in names): continue
            if "放养" in (beast.get("status") or ""): beast["status"] = "休息中"; changed += 1
        if self.state.get("best_beast_status") and "放养" in self.state.get("best_beast_status"):
            best_name = self.state.get("best_beast_name", "")
            if not names or any(self.beast_name_matches(best_name, name) for name in names):
                self.state["best_beast_status"] = "休息中"
        return changed

    def mark_all_pastured_beasts_returned(self):
        """全部放养灵兽标记为已归来"""
        changed = 0
        for beast in self.state.get("beasts_cache", []):
            if "放养" in (beast.get("status") or ""): beast["status"] = "休息中"; changed += 1
        if self.state.get("best_beast_status") and "放养" in self.state.get("best_beast_status"):
            self.state["best_beast_status"] = "休息中"
        return changed

    def clear_pasture_pending(self):
        self.state["pasture_pending_count"] = 0
        self.state["pasture_returned_count"] = 0
        self.state["pasture_pending_since"] = ""

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

    def record_auto_pasture_response(self, f_resp, cache=None, best_name="", best_status=""):
        """解析自动 .一键放养 回复并更新放养状态。"""
        cache = cache if cache is not None else self.state.get("beasts_cache", [])
        if f_resp:
            f_cd = self.parse_wait_time(f_resp)
            if f_cd > 0 or self.is_pasture_success(f_resp):
                next_delay = (f_cd + PASTURE_RETURN_DELAY_SECONDS) if f_cd > 0 else PASTURE_CD_SECONDS
                self.state["last_pasture_time"] = add_seconds_str(now_str(), next_delay - PASTURE_CD_SECONDS)
                self.state["next_pasture_time"] = add_seconds_str(now_str(), next_delay)
                if self.is_pasture_success(f_resp):
                    self.record_pasture_dispatch(f_resp, cache)
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
            notify_unrecognized_response(self, ".一键放养", f_resp, log, "一键放养")
            self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            return False
        self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
        self.save_state()
        return False

    def record_pasture_dispatch(self, response_text, fallback_cache=None):
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
        if best_name and self.is_pastured_status(self.state.get("best_beast_status", "")):
            self.defer_beast_actions_while_pastured(best_name, "pasture dispatch")

    async def handle_pasture_return_event(self, event, text=None, sender=None):
        """处理放养归来事件（被动接收，实时更新归来计数）"""
        msg = event.message
        text = text if text is not None else (msg.text or "")
        sender = sender or await event.get_sender()
        if not self.is_pasture_return_message(text): return False
        if not self.text_targets_self(msg, text): return False
        if sender and not is_game_bot_sender(self, sender): return False
        returned = self.parse_pasture_return_count(text)
        if returned <= 0: return True
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
        self.save_state()
        self.beast_wakeup.set()
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

    def is_focus_beast_ready_for_pasture_response(self, text):
        """检测六翼是否已经保持出战状态，满足一键放养前置条件。"""
        if not text:
            return False
        if self.is_beast_deploy_success(text):
            return True
        return BEAST_FOCUS_NAME in text and "出战中" in text and not self.is_beast_injury_response(text)

    def schedule_pasture_retry(self, retry_seconds=600):
        self.state["next_pasture_time"] = add_seconds_str(now_str(), retry_seconds)
        self.save_state()

    async def ensure_focus_beast_deployed_for_pasture(self):
        """一键放养前确保六翼出战，避免六翼被放养后无法召回。"""
        focus = self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
        if not focus:
            log.info(f"Pasture precheck: {BEAST_FOCUS_NAME} not in cache; trying deploy directly without .我的灵兽 refresh.")

        status = (focus or {}).get("status", "")
        focus_cache_name = (focus or {}).get("full_name") or BEAST_FOCUS_NAME
        if "出战" in status:
            return True
        if self.is_pastured_status(status):
            self.defer_beast_actions_while_pastured(focus_cache_name, "pasture precheck")
            return False

        log.info(f"Pasture precheck: {BEAST_FOCUS_NAME} status is {status or '未知'}, deploying before .一键放养.")
        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {BEAST_FOCUS_NAME}", timeout=60, max_retries=1)
        if self.is_focus_beast_ready_for_pasture_response(deploy_resp):
            self.set_best_beast_status(focus_cache_name, "出战中")
            return True
        if self.is_beast_pastured_response(deploy_resp, focus_cache_name):
            self.defer_beast_actions_while_pastured(focus_cache_name, "pasture deploy response")
            return False

        injury_cd = self.record_beast_injury_from_response(focus_cache_name, deploy_resp, source="pasture")
        if injury_cd >= 0:
            retry_seconds = max(600, injury_cd)
            self.schedule_pasture_retry(retry_seconds)
            log.warning(f"Pasture deferred: {BEAST_FOCUS_NAME} cannot deploy while injured; retry in {retry_seconds}s.")
            return False

        if deploy_resp:
            notify_unrecognized_response(self, f".灵兽出战 {BEAST_FOCUS_NAME}", deploy_resp, log, "一键放养前出战")
        self.schedule_pasture_retry()
        return False

    def is_no_beast_deployed_for_steal_response(self, text):
        """检测偷菜时未出战灵兽的明确失败回复"""
        clean = str(text or "")
        return "尚未派遣任何灵兽出战" in clean or ("无法执行此任务" in clean and "灵兽出战" in clean)

    def handle_beast_deploy_failure_for_steal(self, beast_name, response_text, context):
        """Handle explicit deploy failures before steal without emitting unknown alerts."""
        if not response_text:
            self.set_next_steal_not_before(add_seconds_str(now_str(), 600))
            self.save_state()
            return False
        if self.is_beast_pastured_response(response_text, beast_name):
            self.defer_beast_actions_while_pastured(beast_name, context)
            return True
        injury_cd = self.record_beast_injury_from_response(beast_name, response_text, source="steal")
        if injury_cd >= 0:
            self.defer_beast_action_after_injury("steal", injury_cd)
            log.warning(f"Steal deferred: {beast_name} cannot deploy while injured ({context}).")
            return True
        return False

    def is_abyss_success_response(self, text):
        """检测探渊是否成功"""
        return bool(text and not self.is_abyss_busy_response(text) and any(k in text for k in ["成功", "出发", "进入", "送入", "历练", "击败", "战利品", "带回"]))

    def parse_rest_response_status(self, text):
        """解析灵兽休息后的状态"""
        if not text: return ""
        m = re.search(r"正在[（(]([^）)]+)[）)]", text)
        if m: return m.group(1).strip()
        if any(k in text for k in ["召回", "休养", "休息"]): return "休息中"
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
        self.state["last_abyss_time"] = dt_to_str(chosen - timedelta(seconds=21600))

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
        self.state["last_steal_time"] = dt_to_str(chosen - timedelta(seconds=14400))

    def schedule_abyss_retry(self, retry_seconds=None):
        """安排探渊重试时间"""
        if retry_seconds is None:
            status_check = self.state.get("next_beast_status_check_time", "")
            retry_seconds = int(seconds_until(status_check)) if status_check and is_future(status_check) else 600
            retry_seconds = max(60, retry_seconds)
        self.set_next_abyss_not_before(add_seconds_str(now_str(), retry_seconds))
        self.save_state()

    def record_beast_injury_from_response(self, beast_name, text, source=""):
        """记录灵兽受伤及恢复时间"""
        if not self.is_beast_injury_response(text): return -1
        injury_cd = self.parse_injury_wait_time(text)
        source = source or ""
        injury_status = "重伤" if source == "abyss" and "重伤" in text else "受伤"
        self.state["best_beast_injury_source"] = source
        self.set_best_beast_status(beast_name, injury_status)
        self.record_best_beast_status_timing(injury_status, injury_cd)
        if injury_cd > 0: self.schedule_abyss_retry(injury_cd)
        else: self.schedule_abyss_retry()
        return max(0, injury_cd)

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
        """休息灵兽并解析状态"""
        rest_resp = await self.send_and_wait_feedback(f".灵兽休息 {beast_name}")
        rest_status = self.parse_rest_response_status(rest_resp)
        if rest_status: self.set_best_beast_status(beast_name, rest_status)
        return rest_status, rest_resp

    async def normalize_beast_for_steal(self, beast_name):
        """修复偷菜时的伪受伤状态（切换出战状态）"""
        log.warning(f"Beast stale/fake status detected for steal. Switching {beast_name} to battle once.")
        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {beast_name}", timeout=60, max_retries=1)
        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(beast_name, "出战中"); return True
        if self.handle_beast_deploy_failure_for_steal(beast_name, deploy_resp, "偷菜假状态切换"): return False
        if deploy_resp and not self.is_fake_beast_status_response(deploy_resp): notify_unrecognized_response(self, f".灵兽出战 {beast_name}", deploy_resp, log, "偷菜假状态切换")
        self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); return False

    async def normalize_beast_for_abyss(self, beast_name):
        """
        修复探渊时的伪受伤状态（toggle 出战→休息）。
        先发.灵兽出战切换状态，等3秒后发.灵兽休息回到休息中。
        这种 toggle 刷新了灵兽的实际状态，清除"伪受伤"。
        """
        log.warning(f"Beast stale/fake status detected for abyss. Toggling {beast_name} battle/rest once.")
        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {beast_name}", timeout=60, max_retries=1)
        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(beast_name, "出战中")
        elif self.is_beast_pastured_response(deploy_resp, beast_name):
            self.defer_beast_actions_while_pastured(beast_name, "abyss normalize deploy response")
            return False
        elif deploy_resp and not self.is_fake_beast_status_response(deploy_resp):
            injury_cd = self.record_beast_injury_from_response(beast_name, deploy_resp, source="abyss")
            if injury_cd >= 0: self.defer_beast_action_after_injury("abyss", injury_cd); return False
            notify_unrecognized_response(self, f".灵兽出战 {beast_name}", deploy_resp, log, "探渊假状态出战"); self.schedule_abyss_retry(600); return False
        await asyncio.sleep(3)
        rest_status, rest_resp = await self.rest_beast_for_abyss(beast_name)
        if rest_status == "休息中": return True
        if self.is_beast_pastured_response(rest_resp, beast_name) or self.is_pastured_status(rest_status):
            self.defer_beast_actions_while_pastured(beast_name, "abyss normalize rest response")
            return False
        if rest_resp and not self.is_fake_beast_status_response(rest_resp): notify_unrecognized_response(self, f".灵兽休息 {beast_name}", rest_resp, log, "探渊假状态休息")
        self.schedule_abyss_retry(600); return False

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
            self.defer_beast_actions_while_pastured(beast_name, "abyss response")
            return resp
        if self.is_abyss_busy_response(resp):
            cached = self.get_cached_beast_by_name(beast_name)
            if self.is_pastured_status((cached or {}).get("status", "")) or self.is_pastured_status(self.state.get("best_beast_status", "")):
                self.defer_beast_actions_while_pastured(beast_name, "abyss busy while cached pastured")
                return resp
            log.warning(f"Abyss blocked by stale busy status for {beast_name}. Normalizing once before retry.")
            if not await self.normalize_beast_for_abyss(beast_name): return resp
            await asyncio.sleep(3)
            resp = await self.send_abyss_once(beast_name)
            if self.is_beast_pastured_response(resp, beast_name):
                self.defer_beast_actions_while_pastured(beast_name, "abyss retry response")
            elif self.is_abyss_busy_response(resp): log.warning(f"Abyss still blocked by busy status for {beast_name}; retry later."); self.schedule_abyss_retry(600)
        return resp

    def is_beast_stamina_insufficient_response(self, text):
        clean = str(text or "").replace("**", "")
        return "灵兽" in clean and "体力不足" in clean

    def parse_required_beast_stamina(self, text, default=BEAST_ABYSS_MIN_STAMINA):
        clean = str(text or "").replace("**", "")
        match = re.search(r"至少需要\s*(\d+)\s*点体力", clean)
        return int(match.group(1)) if match else default

    def record_beast_stamina_shortage(self, beast_name, text, action="abyss"):
        required = self.parse_required_beast_stamina(text)
        inferred_stamina = max(0, required - 1)
        self.set_cached_beast_stamina(beast_name, inferred_stamina)
        if action == "abyss":
            self.state["next_beast_status_check_time"] = add_seconds_str(now_str(), 1800)
        log.info(f"Beast {action}: {beast_name} stamina below {required}; trying fallback candidate.")
        self.save_state()
        return required

    def abyss_candidate_beasts(self, cache=None):
        candidates = []
        for beast in self.sorted_beasts_by_power(cache):
            status = beast.get("status", "未知")
            stamina = self.beast_stamina_value(beast)
            if stamina >= 0 and stamina < BEAST_ABYSS_MIN_STAMINA:
                log.info(
                    f"Abyss candidate skipped: {beast.get('full_name')} stamina {stamina} < {BEAST_ABYSS_MIN_STAMINA}."
                )
                continue
            if not self.can_attempt_abyss_status(status):
                log.info(f"Abyss candidate skipped: {beast.get('full_name')} status is {status}.")
                continue
            candidates.append(beast)
        return candidates

    async def execute_abyss_with_fallback(self):
        """探渊前刷新.我的灵兽；按战力候选，体力不足时自动换下一只。"""
        log.info("Abyss: refreshing .我的灵兽 before selecting candidate.")
        if not await self.update_beast_cache():
            log.warning("Abyss: failed to refresh beast cache; retry later.")
            self.schedule_abyss_retry(600)
            return False

        candidates = self.abyss_candidate_beasts(self.state.get("beasts_cache", []))
        if not candidates:
            log.warning("Abyss: no beast candidate has enough stamina/status; retry after status refresh.")
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
                rest_status, rest_resp = await self.rest_beast_for_abyss(best_name)
                if rest_status:
                    best_status = rest_status
                else:
                    log.warning(f"Abyss: {best_name} rest failed before abyss; trying next candidate.")
                    if self.is_beast_stamina_insufficient_response(rest_resp):
                        self.record_beast_stamina_shortage(best_name, rest_resp, "abyss")
                    elif rest_resp and self.is_beast_pastured_response(rest_resp, best_name):
                        self.set_best_beast_status(best_name, "放养中")
                    continue
                await asyncio.sleep(3)
                if not self.can_attempt_abyss_status(best_status):
                    log.warning(f"Abyss: {best_name} cannot enter after recall; status is {best_status}.")
                    continue

            a_resp = await self.send_abyss_with_busy_retry(best_name)
            last_response = a_resp or last_response
            if not a_resp:
                self.schedule_abyss_retry(600)
                return False
            if self.is_beast_stamina_insufficient_response(a_resp):
                self.record_beast_stamina_shortage(best_name, a_resp, "abyss")
                await asyncio.sleep(3)
                continue

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
                self.state["last_abyss_time"] = now_str()
                self.state["next_abyss_time"] = add_seconds_str(now_str(), 21600)
                self.set_best_beast_status(best_name, "休息中")
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
        blocks = re.split(r'\n-\s*', clean)
        for block in blocks:
            if not block.strip() or "灵兽伙伴们" in block: continue
            lines = block.strip().split('\n')
            header = lines[0]
            brackets = re.findall(r'\(([^)]+)\)', header)
            name_base = re.sub(r'\(.*?\)', '', header).replace('-', '').strip()
            status_words = ("出战中", "休息中", "放养中", "受伤", "重伤", "治疗中", "探险中", "偷菜中", "巡游中")
            status = "未知"
            suffix_parts = brackets
            if brackets and any(word in brackets[-1] for word in status_words):
                status = brackets[-1]; suffix_parts = brackets[:-1]
            suffix = " ".join(f"({b})" for b in suffix_parts)
            full_name = f"{name_base} {suffix}".strip()
            species_match = re.search(r'种类[:：]\s*([^\n]+)', block)
            exp_match = re.search(r'经验[:：]\s*(\d+)', block)
            power_match = re.search(r'战力[:：]\s*(\d+)', block)
            stamina_match = re.search(r'体力[:：]\s*(\d+)', block)
            beasts.append({
                'full_name': full_name, 'status': status,
                'status_cd': self.parse_wait_time(block) if any(k in status for k in ["受伤", "治疗"]) else -1,
                'species': species_match.group(1).strip() if species_match else name_base,
                'exp': int(exp_match.group(1)) if exp_match else 0,
                'power': int(power_match.group(1)) if power_match else 0,
                'stamina': int(stamina_match.group(1)) if stamina_match else -1,
            })
        return self.sorted_beasts_by_power(beasts)

    async def update_beast_cache(self):
        """刷新灵兽缓存（发送.我的灵兽并解析结果）"""
        log.info("Refreshing Beast Cache...")
        resp = await self.send_and_wait_feedback(".我的灵兽")
        if resp and "灵兽" in resp:
            beasts = self.parse_beasts_info(resp)
            self.state["beasts_cache"] = beasts
            self.update_best_beast_tracking()
            self.should_stop_hunt_by_tenth_beast(beasts)
            self.save_state()
            summary = ", ".join(f"{b['full_name']}({b['power']},体力{b.get('stamina', -1)})" for b in beasts)
            log.info(f"Beast Cache: {len(beasts)} parsed: {summary}")
            return True
        return False

    # ---- 关键词提醒 ----

    def should_send_keyword_alert(self, msg, text):
        """判断是否应发送关键词告警"""
        if not text: return False
        lower_text = text.lower()
        return any(k in lower_text for k in self.keywords) and (mentions_self(self, msg, text) or any(f"@{u}" in lower_text or f"【{u}】" in lower_text for u in self.notify_users))

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
            if is_game_bot_sender(self, sender): record_game_bot_activity(self, sender, log)
            # 身外化身：被动身份自愈更新
            if is_game_bot_sender(self, sender): 
                self.update_identity_passively(msg)
                await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                self.maybe_record_avatar_passive_states(msg)
            if await handle_anti_bot_challenge(self, msg, text, sender, log, title="万灵宗自证告警"): return
            if is_game_bot_sender(self, sender) and self.should_send_keyword_alert(msg, text): await self.send_keyword_alert(msg, text, title="万灵宗关键词提醒")
            self.maybe_record_field_training_passive(msg, text)
            self.maybe_handle_sect_war_message(msg, text, sender)
            if is_game_bot_sender(self, sender): self.maybe_record_manual_pasture_dispatch(msg, text)
            self.remember_manual_pasture_command_if_needed(msg, text)
            
            # --- 阵法助阵拦截 ---
            if is_game_bot_sender(self, sender) and "周天星斗大阵-启" in text and "正在布设大阵" in text and ("尚需" in text or "助阵" in text):
                asyncio.create_task(self.handle_global_formation_invite(msg.id))
                    
            # --- 观星显化拦截（轮换派发：每次只派一个化身） ---
            if is_game_bot_sender(self, sender) and "【Good -" in text:
                asyncio.create_task(self.avatar_handle_star_gazing_opportunity(None, msg, text, sender))

            # ---- 控制指令：止/启（仅管理员可触发） ----
            # 必须在 log_manual_outgoing_if_needed 之前，否则手动发的"止"会被拦截
            # chat_id 比较需兼容 Telethon 的 -100 前缀（supergroup）
            _chat_id_match = (msg.chat_id == self.target_chat_id or msg.chat_id == int(f"-100{self.target_chat_id}"))
            if not is_game_bot_sender(self, sender) and _chat_id_match:
                _sender_id = getattr(msg, "sender_id", None)
                if _sender_id and _sender_id in self.pause_admins:
                    stripped = text.strip()
                    if stripped in ("止", ".止", "0", ".0"):
                        if self.pause_event.is_set():
                            self.pause_event.clear()
                            self.state["is_paused"] = True
                            self.save_state()
                            log.info("⏸️ PAUSE command received. All loops paused.")
                            await self.client.send_message(8219248252, "⏸️ 万灵宗脚本已暂停。发送「1」恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return
                    elif stripped in ("启", ".启", "1", ".1"):
                        if not self.pause_event.is_set():
                            self.pause_event.set()
                            self.state["is_paused"] = False
                            self.save_state()
                            log.info("▶️ RESUME command received. All loops resumed.")
                            await self.client.send_message(8219248252, "▶️ 万灵宗脚本已恢复运行。")
                        try: await self.client.delete_messages(self.target_chat_id, msg)
                        except: pass
                        return

            if log_manual_outgoing_if_needed(self, msg, text=text): return
            if is_auto_reply_followup(self, msg, sender=sender): return

            is_matched = False
            # 1. 回复匹配（最优先）
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
                        if cmd_text == ".查看闭关" and not self.is_loose_meditation_feedback_candidate(cmd_text, text):
                            log.warning(f"Reply match rejected for [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                            _skip_reply = True
                        if not _skip_reply:
                            self.last_feedback_text[replied_id] = text; self.last_feedback_msg[replied_id] = msg; self.feedback_events[replied_id].set(); is_matched = True
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
                    )
            if not is_matched:
                log_mention_if_needed(self, msg, text=text, sender=sender)
                if "黄枫谷" in text and "药园" in text: return
                if await maybe_auto_reply_exchange(self, event, text=text): return
        except Exception as e: log.error(f"Handler Error: {e}")

    # ---- 互相助阵：缘生子 ↔ 素心子 ----

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

            # 检查深度闭关
            in_med = a_state.get("in_deep_meditation", False)
            end_time = a_state.get("deep_meditation_end_time", "")
            if in_med and end_time and is_future(end_time):
                log.info(f"[互助阵] {partner} in deep meditation until {end_time}, skipping assist for {initiator}")
                return

            log.info(f"[互助阵] {initiator} 启阵 → {partner} 助阵 (msg_id={msg_id})")
            resp = await self.send_and_wait_feedback_identity(partner, ".助阵", reply_to=msg_id)
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

    async def handle_global_formation_invite(self, msg_id):
        """处理全局阵法邀请，只让一个化身去助阵，失败则尝试下一个。"""
        # 前置校验：是否在全局助阵封禁期间
        form_ban = self.state.get("next_formation_ban_time", "")
        if form_ban and is_future(form_ban):
            log.warning(f"🚫 Global formation assist command is banned until {form_ban}. Ignoring invite.")
            return

        now = time.monotonic()
        if getattr(self, "last_global_assist_time", 0) and now - self.last_global_assist_time < 60:
            return
        self.last_global_assist_time = now

        for avatar in ["素心子", "缘生子"]:
            a_state = self.get_avatar_state(avatar)

            # 前置校验 1：如果已经在深度闭关中，跳过
            in_med = a_state.get("in_deep_meditation", False)
            end_time = a_state.get("deep_meditation_end_time", "")
            if in_med and end_time and is_future(end_time):
                log.info(f"Avatar {avatar} skipped: currently in deep meditation until {end_time}")
                continue

            # 前置校验 2：大阵冷却时间未到，跳过
            next_form = a_state.get("next_formation_time", "")
            if next_form and is_future(next_form):
                log.info(f"Avatar {avatar} skipped: formation CD active until {next_form}")
                continue

            log.info(f"Avatar {avatar}: sending .助阵 to invite message {msg_id}")
            resp = await self.send_and_wait_feedback_identity(avatar, ".助阵", reply_to=msg_id)
            resp_str = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""

            # 校验是否被大阵助阵命令保护拦截
            if resp_str and any(k in resp_str for k in ["命令保护提醒", "已暂停该命令", "暂停该命令"]):
                cd = self.parse_wait_time(resp_str)
                cd_seconds = cd if cd > 0 else 3600
                ban_expire = add_seconds_str(now_str(), cd_seconds)
                self.state["next_formation_ban_time"] = ban_expire
                self.save_state()
                log.critical(f"⚠️ Global Formation Assist Command Banned! Suspended until {ban_expire}. Response: {resp_str}")
                return # 全局被禁，直接退出

            if resp_str:
                if any(k in resp_str for k in ["助阵成功", "大阵已成", "成功助阵", "阵成", "加入大阵", "已经参与"]):
                    log.info(f"Avatar {avatar} successfully assisted formation!")
                    cd = self.parse_wait_time(resp_str)
                    cd_seconds = cd if cd > 0 else 7200 # 成功默认 2 小时
                    self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd_seconds))
                    self.save_state()
                    return  # 只要有一人成功助阵，就直接退出
                else:
                    log.info(f"Avatar {avatar} assist failed/response: {resp_str[:120]}")
                    # 即使失败了，如果提示已经在冷却中，也需要更新冷却时间
                    cd = self.parse_wait_time(resp_str)
                    if cd > 0 or any(k in resp_str for k in ["冷却", "尚未结束", "请在", "未到"]):
                        cd_seconds = cd if cd > 0 else 1800 # 冷却默认 30 分钟
                        self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd_seconds))
                        self.save_state()
            # 如果助阵失败（例如在冷却中），稍作延迟后换下一个化身尝试
            await asyncio.sleep(2)

    # ---- 观星与改换星移 (化身专用) ----

    def star_gazing_good_opportunity(self, text):
        return bool(text and any(keyword in text for keyword in STAR_GAZING_GOOD_KEYWORDS))

    def get_avatar_username(self, avatar):
        """根据化身名称反向查找其 Telegram 用户名"""
        for uname, name in self.avatar_usernames.items():
            if name == avatar:
                return uname
        return ""

    def next_star_manifest_dt(self, now=None):
        """下一次星象显现候选时间（每 3 小时一次：0:00, 3:00, 6:00, ...）"""
        now = now or datetime.now()
        base_hour = (now.hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
        candidate = now.replace(hour=base_hour, minute=0, second=0, microsecond=0)
        if now >= candidate:
            candidate += timedelta(hours=STAR_GAZING_INTERVAL_HOURS)
        return candidate

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
        return (
            self.state.get("star_gazing_claimed_manifest_time", "") == manifest_key
            and self.state.get("star_gazing_claimed_avatar", "") == avatar
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

    @safe_bg_task
    async def avatar_schedule_star_shift(self, avatar, reply_msg_id, target_dt, gazing_date=None):
        today = gazing_date or target_dt.strftime("%Y-%m-%d")
        shift_dt = target_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)  # 即 target_dt + 25秒
        if self.get_avatar_state(avatar).get("last_star_shift_date") == today: return
        if datetime.now() > shift_dt + timedelta(seconds=5): return
        
        wait_sec = (shift_dt - datetime.now()).total_seconds()
        if wait_sec > 0: await asyncio.sleep(wait_sec)
        
        if self.get_avatar_state(avatar).get("last_star_shift_date") == today: return

        self.active_atomic_task = asyncio.current_task()
        log.info(f"🔒 [ATOMIC LOCK] Acquired by AvatarStarShift-{avatar}")
        try:
            uname = self.get_avatar_username(avatar)
            if not uname:
                log.error(f"Avatar {avatar}: username mapping not found; skipping star shift.")
                return

            command = f".改换星移 @{STAR_SHIFT_TARGET}"
            log.info(f"Avatar {avatar} Star gazing: sending {command} as reply to .观星 result {reply_msg_id}.")
            sent_msg = await self.send_and_wait_feedback_identity(avatar, command, reply_to=reply_msg_id)
            if sent_msg:
                self.set_avatar_state(avatar, "last_star_shift_date", today)
                self.set_avatar_state(avatar, "last_star_shift_time", now_str())
        finally:
            if self.active_atomic_task == asyncio.current_task():
                self.active_atomic_task = None
                log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarShift-{avatar}")

    async def avatar_schedule_star_gazing_simple(self, avatar, send_dt, immediate_shift=False, manifest_dt=None):
        now = datetime.now()
        wait_sec = (send_dt - now).total_seconds()
        if wait_sec > 0: await asyncio.sleep(wait_sec)

        today = datetime.now().strftime("%Y-%m-%d")
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
            resp_msg = await self.send_and_wait_feedback_identity(avatar, ".观星", timeout=20, max_retries=0, return_response_msg=True, delete_after=False)

            self.set_avatar_state(avatar, "last_gazing_date", today)
            self.set_avatar_state(avatar, "last_gazing_time", now_str())
            if not resp_msg: return

            resp_text = (resp_msg.text or "")
            if self.star_gazing_good_opportunity(resp_text):
                if immediate_shift:
                    current_manifest_hour = (datetime.now().hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
                    current_manifest_dt = datetime.now().replace(
                        hour=current_manifest_hour, minute=0, second=0, microsecond=0
                    )
                    shift_dt = current_manifest_dt - timedelta(seconds=STAR_GAZING_SHIFT_LEAD_SECONDS)

                    now2 = datetime.now()
                    if now2 < shift_dt:
                        wait_sec = (shift_dt - now2).total_seconds()
                        log.info(
                            f"Avatar {avatar} Star gazing: GOOD result during ACTIVE window, "
                            f"but too early for shift. Waiting {wait_sec:.1f}s until {dt_to_str(shift_dt)}."
                        )
                        await asyncio.sleep(wait_sec)

                    uname = self.get_avatar_username(avatar)
                    if not uname:
                        log.error(f"Avatar {avatar}: username mapping not found in immediate mode; skipping shift.")
                        return
                    command = f".改换星移 @{STAR_SHIFT_TARGET}"
                    log.info(f"Avatar {avatar} Star gazing: sending {command} in ACTIVE window as reply to msg {resp_msg.id}.")
                    sent_msg = await self.send_and_wait_feedback_identity(avatar, command, reply_to=resp_msg.id)
                    if sent_msg:
                        self.set_avatar_state(avatar, "last_star_shift_date", today)
                        self.set_avatar_state(avatar, "last_star_shift_time", now_str())
                else:
                    target_dt = self.next_star_manifest_dt(datetime.now())
                    target_day = target_dt.strftime("%Y-%m-%d")
                    if self.get_avatar_state(avatar).get("last_star_shift_date") != target_day:
                        log.info(f"Avatar {avatar} Star gazing: GOOD result; scheduling .改换星移 before {dt_to_str(target_dt)}.")
                        asyncio.create_task(self.avatar_schedule_star_shift(avatar, resp_msg.id, target_dt, today))
            else:
                log.info(f"Avatar {avatar} Star gazing: .观星 result does not contain GOOD keyword; skipping .改换星移.")
        finally:
            if self.active_atomic_task == asyncio.current_task():
                self.active_atomic_task = None
                log.info(f"🔓 [ATOMIC LOCK] Released by AvatarStarGazing-{avatar}")

    async def avatar_handle_star_gazing_opportunity(self, avatar, msg, text, sender):
        is_our_good = self.star_gazing_good_opportunity(text)
        is_any_good = bool(text and "【Good -" in text)

        if not is_any_good: return
        if not sender or not is_game_bot_sender(self, sender): return

        now = datetime.now()
        if now.hour < STAR_GAZING_OPPORTUNITY_START_HOUR:
            log.info(f"Avatar Star gazing: ignoring manifest message before {STAR_GAZING_OPPORTUNITY_START_HOUR}:00 (likely stale).")
            return

        today = now.strftime("%Y-%m-%d")
        current_manifest_hour = (now.hour // STAR_GAZING_INTERVAL_HOURS) * STAR_GAZING_INTERVAL_HOURS
        current_manifest_dt = now.replace(hour=current_manifest_hour, minute=0, second=0, microsecond=0)
        window_active_until = current_manifest_dt + timedelta(seconds=STAR_GAZING_ACTIVE_WINDOW_SECONDS)

        if is_our_good:
            if now <= window_active_until:
                manifest_dt = current_manifest_dt
                send_dt = now + timedelta(seconds=3)
                immediate_shift = True
            else:
                manifest_dt = self.next_star_manifest_dt(now)
                send_dt = manifest_dt - timedelta(minutes=1)
                immediate_shift = False

            async with self.star_gazing_lock:
                manifest_key = dt_to_str(manifest_dt)
                claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
                claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
                if claimed_manifest == manifest_key and claimed_avatar:
                    log.info(
                        f"Avatar Star gazing: manifest {manifest_key} already assigned to {claimed_avatar}; "
                        "skip duplicate trigger."
                    )
                    return

                selected_avatar = avatar
                idx = 0
                if not selected_avatar:
                    selected_avatar, idx = self.choose_star_gazing_avatar_for_today(today)
                if not selected_avatar:
                    log.info("Avatar Star gazing: all rotating avatars already observed today; skipping.")
                    return
                if self.get_avatar_state(selected_avatar).get("last_gazing_date") == today:
                    return

                self.state["star_gazing_claimed_manifest_time"] = manifest_key
                self.state["star_gazing_claimed_avatar"] = selected_avatar
                self.state["pending_star_gazing_manifest_time"] = manifest_key
                self.set_avatar_state(selected_avatar, "pending_star_gazing_date", today)
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

    # ---- 分身星辰牵引 / 安抚 / 收集 ----

    def response_text(self, resp):
        if hasattr(resp, "text"):
            return resp.text or ""
        if isinstance(resp, str):
            return resp
        return str(resp) if resp else ""

    def recent_command_guard_wait(self, command="", max_age_seconds=15):
        block = getattr(self, "_last_command_guard_block", {}) or {}
        at = block.get("at", 0) or 0
        if not at or time.monotonic() - at > max_age_seconds:
            return 0
        key = str(block.get("key", "") or "")
        if command and not key.startswith(str(command).strip()):
            return 0
        wait = int(block.get("wait", 0) or 0)
        blocked_until = float(block.get("blocked_until", 0) or 0)
        if blocked_until > time.monotonic():
            wait = max(wait, int(blocked_until - time.monotonic()))
        return max(0, wait)

    def apply_avatar_star_guard_backoff(self, avatar, command="", fields=None, reason="command guard"):
        wait = self.recent_command_guard_wait(command)
        if wait <= 0:
            return False
        retry_at = add_seconds_str(now_str(), max(60, wait + 5))
        updates = {"next_star_check_time": retry_at}
        for field in fields or []:
            updates[field] = retry_at
        if not command or str(command).startswith(".观星台"):
            updates["star_observatory_needs_refresh"] = True
        self.update_avatar_states(avatar, updates)
        key = (getattr(self, "_last_command_guard_block", {}) or {}).get("key", command)
        log.warning(f"Avatar [{avatar}] star cycle backed off by command guard [{key}] until {retry_at}.")
        return True

    def compact_seconds(self, seconds):
        seconds = max(0, int(seconds or 0))
        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}小时{minutes}分钟"
        if minutes:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"

    def avatar_star_due(self, avatar, key):
        value = self.get_avatar_state(avatar).get(key, "")
        return bool(value and not is_future(value))

    def parse_avatar_star_observatory(self, text):
        clean = (text or "").replace("**", "").replace("`", "")
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        disk_lines = [
            line for line in lines
            if "引星盘" in line
            and ":" in line
            and "观星台" not in line
            and "总数" not in line
            and not line.startswith("使用")
        ]
        valid = "【星宫 · 观星台】" in clean or ("观星台" in clean and bool(disk_lines))
        if not valid and not disk_lines:
            return {"valid": False}

        total_match = re.search(r"总数\s*[:：]\s*(\d+)\s*座", clean)
        total_count = int(total_match.group(1)) if total_match else len(disk_lines)
        empty_count = 0
        occupied_count = 0
        collect_ready = False
        needs_appease = False
        remaining_values = []
        stars = set()

        for line in disk_lines:
            status = line.split(":", 1)[1].strip()
            if "空闲" in status:
                empty_count += 1
                continue
            occupied_count += 1
            star_match = re.search(r":\s*([^-:：\n]+?)\s*(?:-|$)", line)
            if star_match:
                star_name = star_match.group(1).strip()
                if star_name and "空闲" not in star_name:
                    stars.add(star_name)
            if "精华已成" in status:
                collect_ready = True
            if any(k in status for k in ["星光黯淡", "元磁紊乱", "狂暴星力", "黯淡", "紊乱"]):
                needs_appease = True
            cd = self.parse_wait_time(line)
            if cd and cd > 0 and "剩余" in line:
                remaining_values.append(cd)

        min_remaining = min(remaining_values) if remaining_values else None
        if collect_ready:
            summary = "精华已成"
        elif min_remaining is not None:
            summary = f"凝聚中，剩余{self.compact_seconds(min_remaining)}"
        elif needs_appease:
            summary = "需安抚"
        elif total_count and empty_count >= total_count:
            summary = "全部空闲"
        elif occupied_count:
            summary = "已占用，待复查"
        else:
            summary = "未知"

        return {
            "valid": True,
            "total_count": total_count,
            "empty_count": empty_count,
            "occupied_count": occupied_count,
            "collect_ready": collect_ready,
            "needs_appease": needs_appease,
            "min_remaining": min_remaining,
            "stars": sorted(stars),
            "summary": summary,
        }

    def record_avatar_star_observatory(self, avatar, text, source="观星台"):
        info = self.parse_avatar_star_observatory(text)
        if not info.get("valid"):
            return False

        now = now_str()
        updates = {
            "last_star_observatory_time": now,
            "star_observatory_summary": info.get("summary", ""),
            "star_observatory_needs_refresh": False,
            "star_target": STAR_ATTRACTION_TARGET,
        }

        min_remaining = info.get("min_remaining")
        if info.get("collect_ready"):
            updates["next_star_collect_time"] = now
            if info.get("needs_appease"):
                updates["next_star_appease_time"] = now
            updates["next_star_check_time"] = now
        elif min_remaining is not None:
            collect_time = add_seconds_str(now, min_remaining)
            appease_delay = max(0, min_remaining - STAR_PRE_APPEASE_LEAD_SECONDS)
            appease_time = add_seconds_str(now, appease_delay)
            updates["next_star_collect_time"] = collect_time
            updates["next_star_appease_time"] = appease_time
            updates["next_star_attraction_time"] = collect_time
            updates["next_star_check_time"] = updates["next_star_appease_time"]
        elif info.get("total_count") and info.get("empty_count", 0) >= info.get("total_count", 0):
            updates["next_star_collect_time"] = ""
            updates["next_star_appease_time"] = ""
            if not is_future(self.get_avatar_state(avatar).get("next_star_attraction_time", "")):
                updates["next_star_attraction_time"] = now
                updates["next_star_check_time"] = now
            else:
                updates["next_star_check_time"] = self.get_avatar_state(avatar).get("next_star_attraction_time", "")
        elif info.get("needs_appease"):
            updates["next_star_appease_time"] = now
            updates["next_star_check_time"] = now
        else:
            updates["next_star_check_time"] = add_seconds_str(now, STAR_STATUS_RETRY_SECONDS)

        self.update_avatar_states(avatar, updates)
        log.info(f"Avatar [{avatar}] star observatory synced ({source}): {info.get('summary', '')}")
        return True

    def _star_cycle_collect_time_from_now(self):
        return add_seconds_str(now_str(), STAR_ATTRACTION_COOLDOWN_SECONDS)

    def record_avatar_star_pull_response(self, avatar, text, source=STAR_ATTRACTION_COMMAND):
        if not text:
            retry_at = add_seconds_str(now_str(), STAR_STATUS_RETRY_SECONDS)
            self.update_avatar_states(avatar, {
                "star_attraction_retry_time": retry_at,
                "next_star_attraction_time": retry_at,
                "next_star_check_time": retry_at,
            })
            return "empty"

        clean = text.replace("**", "")
        now = now_str()
        if "修为不足" in clean:
            self.update_avatar_states(avatar, {
                "star_attraction_retry_time": now,
                "next_star_attraction_time": now,
            })
            return "insufficient"

        if "已无空闲" in clean and "引星盘" in clean:
            self.update_avatar_states(avatar, {
                "star_observatory_needs_refresh": True,
                "next_star_check_time": now,
            })
            return "no_free"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            due = add_seconds_str(now, cd)
            appease_time = add_seconds_str(now, max(0, cd - STAR_PRE_APPEASE_LEAD_SECONDS))
            self.update_avatar_states(avatar, {
                "next_star_attraction_time": due,
                "next_star_collect_time": due,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
                "star_attraction_retry_time": "",
                "star_attraction_force_exit_tried": False,
            })
            return "cooldown"

        if any(k in clean for k in ["牵引成功", "成功在", "牵引了", "开始牵引"]):
            collect_time = self._star_cycle_collect_time_from_now()
            appease_time = add_seconds_str(collect_time, -STAR_PRE_APPEASE_LEAD_SECONDS)
            self.update_avatar_states(avatar, {
                "last_star_attraction_time": now,
                "next_star_attraction_time": collect_time,
                "next_star_collect_time": collect_time,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
                "star_attraction_retry_time": "",
                "star_attraction_force_exit_tried": False,
                "star_observatory_needs_refresh": False,
                "star_observatory_summary": f"{STAR_ATTRACTION_TARGET}凝聚中",
            })
            return "success"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
        })
        notify_unrecognized_response(self, STAR_ATTRACTION_COMMAND, text, log, source)
        return "unknown"

    def record_avatar_star_appease_response(self, avatar, text, source=".安抚星辰"):
        now = now_str()
        clean = (text or "").replace("**", "")
        if clean and any(k in clean for k in ["成功安抚", "安抚了", "没有需要安抚", "无需安抚", "不需要安抚"]):
            state = self.get_avatar_state(avatar)
            next_collect = state.get("next_star_collect_time", "")
            self.update_avatar_states(avatar, {
                "last_star_appease_time": now,
                "next_star_appease_time": "",
                "next_star_check_time": next_collect if next_collect else add_seconds_str(now, STAR_STATUS_RETRY_SECONDS),
            })
            return "success"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            due = add_seconds_str(now, cd)
            self.update_avatar_states(avatar, {
                "next_star_appease_time": due,
                "next_star_check_time": due,
            })
            return "cooldown"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
        })
        if text:
            notify_unrecognized_response(self, ".安抚星辰", text, log, source)
        return "unknown"

    def record_avatar_star_collect_response(self, avatar, text, source=".收集精华"):
        now = now_str()
        clean = (text or "").replace("**", "")
        if clean and any(k in clean for k in ["收集完成", "成功从", "获得了"]):
            self.update_avatar_states(avatar, {
                "last_star_collect_time": now,
                "next_star_collect_time": "",
                "next_star_appease_time": "",
                "next_star_attraction_time": now,
                "next_star_check_time": now,
                "star_observatory_needs_refresh": False,
                "star_attraction_force_exit_tried": False,
            })
            return "success"

        if clean and any(k in clean for k in ["没有已凝聚", "没有可供收集", "暂无精华"]):
            self.update_avatar_states(avatar, {
                "star_observatory_needs_refresh": True,
                "next_star_check_time": now,
            })
            return "not_ready"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            collect_time = add_seconds_str(now, cd)
            appease_time = add_seconds_str(now, max(0, cd - STAR_PRE_APPEASE_LEAD_SECONDS))
            self.update_avatar_states(avatar, {
                "next_star_collect_time": collect_time,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
            })
            return "cooldown"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
        })
        if text:
            notify_unrecognized_response(self, ".收集精华", text, log, source)
        return "unknown"

    def record_avatar_star_response_from_text(self, avatar, text, source="star sync"):
        if avatar not in STAR_ATTRACTION_AVATARS or not text:
            return False
        clean = text.replace("**", "")
        if "已无空闲" in clean and "引星盘" in clean:
            return self.record_avatar_star_pull_response(avatar, text, source) in {"no_free"}
        if "观星台" in clean and "引星盘" in clean:
            return self.record_avatar_star_observatory(avatar, text, source)
        if (
            any(k in clean for k in ["牵引成功", "牵引了", "开始牵引", "牵引星辰"])
            or ("修为不足" in clean and STAR_ATTRACTION_TARGET in clean)
            or ("冷却" in clean and "牵引" in clean)
        ):
            return self.record_avatar_star_pull_response(avatar, text, source) in {"success", "cooldown", "no_free", "insufficient"}
        if "安抚" in clean and ("引星盘" in clean or "星辰" in clean or "狂暴星力" in clean):
            return self.record_avatar_star_appease_response(avatar, text, source) in {"success", "cooldown"}
        if "收集" in clean or "精华" in clean:
            return self.record_avatar_star_collect_response(avatar, text, source) in {"success", "cooldown", "not_ready"}
        return False

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

                if self.avatar_star_due(avatar, "next_star_appease_time"):
                    await self.attempt_avatar_star_appease(avatar)
                    continue

                if self.avatar_star_due(avatar, "next_star_collect_time"):
                    state = self.get_avatar_state(avatar)
                    if state.get("next_star_appease_time") and not is_future(state.get("next_star_appease_time")):
                        await self.attempt_avatar_star_appease(avatar)
                        continue
                    await self.attempt_avatar_star_collect(avatar)
                    continue

                if self.avatar_star_due(avatar, "next_star_attraction_time") and not is_future(state.get("next_star_collect_time", "")):
                    await self.attempt_avatar_star_pull(avatar)
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
        state = self.get_avatar_state(avatar)
        check_time = state.get("next_star_check_time", "")
        if state.get("star_observatory_needs_refresh") and (not check_time or not is_future(check_time)):
            return 0
        if (
            not state.get("last_star_observatory_time")
            and not state.get("next_star_collect_time")
            and not is_future(state.get("next_star_attraction_time", ""))
        ):
            return 0

        waits = []
        for key in [
            "star_attraction_retry_time",
            "next_star_check_time",
            "next_star_appease_time",
            "next_star_collect_time",
            "next_star_attraction_time",
        ]:
            value = state.get(key, "")
            if not value:
                continue
            if is_future(value):
                waits.append(seconds_until(value))
            else:
                waits.append(0)
        if not waits:
            return 0
        return max(0, min(waits))

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

    async def run_beast_hunt_timer(self):
        """
        寻觅灵兽主循环。
        策略：
        1. 保持最多10只灵兽
        2. 第10只为风雀时停止狩猎
        3. 满10只时放生第10只（最低战力）再继续狩猎
        4. 不主动刷新灵兽面板；灵兽缓存只在探渊前更新
        """
        await self.startup_done.wait()
        while self.is_running:
            if self.state.get("beast_hunt_stopped"):
                log.info(f"Hunt Loop: stopped ({self.state.get('beast_hunt_stopped_reason', '')}).")
                await asyncio.sleep(24 * 3600); continue
            next_hunt = self.state.get("next_hunt_time", "")
            if next_hunt and is_future(next_hunt):
                await asyncio.sleep(seconds_until(next_hunt) + random.randint(10, 30)); continue
            last_run = self.state.get("last_hunt_time", "")
            if last_run and is_future(add_seconds_str(last_run, HUNT_CD_SECONDS)):
                await asyncio.sleep(seconds_until(add_seconds_str(last_run, HUNT_CD_SECONDS)) + random.randint(10, 30)); continue
            async with self.beast_lock:
                next_hunt = self.state.get("next_hunt_time", ""); last_run = self.state.get("last_hunt_time", "")
                if next_hunt and is_future(next_hunt): continue
                if last_run and is_future(add_seconds_str(last_run, HUNT_CD_SECONDS)): continue
                cache = list(self.state.get("beasts_cache", []))
                if self.should_stop_hunt_by_tenth_beast(cache): continue
                if len(cache) >= 10:
                    last_rel = self.state.get("last_release_beast_time", "2000-01-01 00:00:00")
                    if (datetime.now() - str_to_dt(last_rel)).total_seconds() > 600:
                        release_target = self.tenth_beast(cache)
                        if not release_target: self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS); self.save_state(); continue
                        rel_resp = await self.send_and_wait_feedback(f".放生 {release_target['full_name']}")
                        if rel_resp and any(k in rel_resp for k in ["解除", "放生", "回归"]):
                            self.state["last_release_beast_time"] = now_str()
                            self.state["beasts_cache"] = [b for b in cache if b.get("full_name") != release_target.get("full_name")]
                            self.save_state()
                            await asyncio.sleep(3)
                        elif rel_resp and self.is_no_such_beast_response(rel_resp):
                            self.state["beasts_cache"] = [b for b in cache if b.get("full_name") != release_target.get("full_name")]
                            self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FULL_RETRY_SECONDS)
                            self.save_state()
                        else:
                            if rel_resp: notify_unrecognized_response(self, f".放生 {release_target['full_name']}", rel_resp, log, "寻觅前放生第十只")
                            self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS)
                            self.save_state()
                    else: log.warning("Hunt: Release safety lock active, skipping release.")
                    if len(self.state.get("beasts_cache", [])) >= 10: log.warning("Hunt: Still >= 10 after release; skipping .寻觅灵兽."); await asyncio.sleep(300); continue
                resp = await self.send_and_wait_feedback(".寻觅灵兽")
                if resp:
                    cd = self.parse_wait_time(resp)
                    if self.is_beast_bag_full_response(resp):
                        log.warning("Hunt: Beast bag is full. Retry later; beast cache refresh is limited to abyss precheck.")
                        self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FULL_RETRY_SECONDS)
                    elif cd > 0: self.state["last_hunt_time"] = add_seconds_str(now_str(), cd - HUNT_CD_SECONDS); self.state["next_hunt_time"] = add_seconds_str(now_str(), cd)
                    elif any(k in resp for k in ["成功", "出发", "抓到", "寻觅", "搜寻"]):
                        self.state["last_hunt_time"] = now_str(); self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_CD_SECONDS)
                        if self.is_wind_sparrow(resp): self.stop_beast_hunt("寻觅到了风雀")
                    else: notify_unrecognized_response(self, ".寻觅灵兽", resp, log, "寻觅灵兽"); self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS)
                    self.save_state()
                else: self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS); self.save_state()
            await asyncio.sleep(60)

    # ---- 主循环：灵兽行动（探渊 + 偷菜 + 互动/巡游） ----

    async def run_beast_action_timer(self):
        """
        灵兽行动主循环。
        按优先级执行：探渊(6h) → 偷菜(4h) → 一键放养(4h) → 灵兽互动(90min) → 灵兽巡游(120min)。
        探渊和偷菜使用最高战力灵兽；互动/巡游固定使用六翼。
        """
        await self.startup_done.wait()
        while self.is_running:
            # 化身正在发送命令时等待，避免以错误身份发送灵兽命令
            if self.avatar_send_lock.locked():
                log.info("Beast timer deferred: avatar_send_lock held. Waiting 30s.")
                await asyncio.sleep(30)
                continue
            sleep_for = 600
            async with self.beast_lock:
                last_abyss = self.state.get("last_abyss_time", "")
                need_abyss = not last_abyss or not is_future(add_seconds_str(last_abyss, 21600))
                last_steal = self.state.get("last_steal_time", "")
                need_steal = not last_steal or not is_future(add_seconds_str(last_steal, 14400))
                last_pasture = self.state.get("last_pasture_time", "")
                next_pasture = self.state.get("next_pasture_time", "")
                need_pasture = not next_pasture or not is_future(next_pasture)
                if need_pasture and last_pasture:
                    need_pasture = not is_future(add_seconds_str(last_pasture, PASTURE_CD_SECONDS))
                if need_pasture and self.has_pending_pasture_return():
                    self.state["next_pasture_time"] = add_seconds_str(now_str(), 600)
                    self.save_state()
                    need_pasture = False
                last_interaction = self.state.get("last_beast_interaction_time", "")
                next_interaction = self.state.get("next_beast_interaction_time", "")
                need_interaction = not next_interaction or not is_future(next_interaction)
                if need_interaction and last_interaction:
                    need_interaction = not is_future(add_seconds_str(last_interaction, BEAST_INTERACTION_CD_SECONDS))
                last_cruise = self.state.get("last_beast_cruise_time", "")
                next_cruise = self.state.get("next_beast_cruise_time", "")
                need_cruise = not next_cruise or not is_future(next_cruise)
                if need_cruise and last_cruise:
                    need_cruise = not is_future(add_seconds_str(last_cruise, BEAST_CRUISE_CD_SECONDS))
                due_any = need_abyss or need_steal or need_pasture or need_interaction or need_cruise
                if not due_any:
                    next_waits = []
                    for last_time, cd in (
                        (last_abyss, 21600),
                        (last_steal, 14400),
                        (last_pasture, PASTURE_CD_SECONDS),
                        (last_interaction, BEAST_INTERACTION_CD_SECONDS),
                        (last_cruise, BEAST_CRUISE_CD_SECONDS),
                    ):
                        if last_time:
                            next_time = add_seconds_str(last_time, cd)
                            if is_future(next_time): next_waits.append(seconds_until(next_time))
                    for next_time in (next_pasture, next_interaction, next_cruise):
                        if next_time and is_future(next_time):
                            next_waits.append(seconds_until(next_time))
                    if next_waits: sleep_for = max(30, min(next_waits) + random.randint(10, 30))
                else: sleep_for = 30
                cache = list(self.state.get("beasts_cache", []))
                if due_any:
                    if need_abyss or need_steal:
                        if cache:
                            cache.sort(key=lambda x: (x.get('power', 0), x.get('exp', 0), x.get('full_name', '')), reverse=True)
                            best = cache[0]; best_name = best['full_name']
                            if self.state.get("best_beast_name") != best_name: self.update_best_beast_tracking(best); self.save_state()
                            best_status = self.state.get("best_beast_status") or best.get("status", "未知")
                            if need_abyss:
                                await self.execute_abyss_with_fallback()
                                tracked_name = self.state.get("best_beast_name", "")
                                tracked = self.get_cached_beast_by_name(tracked_name) if tracked_name else None
                                if tracked:
                                    best = tracked
                                    best_name = tracked_name
                                best_status = self.state.get("best_beast_status") or best.get("status", "未知")
                                await asyncio.sleep(3)
                            if need_steal:
                                if not self.can_attempt_steal_status(best_status):
                                    status_check = self.state.get("next_beast_status_check_time", "")
                                    if self.is_pastured_status(best_status): self.defer_beast_actions_while_pastured(best_name, "steal status")
                                    elif status_check and is_future(status_check): self.set_next_steal_not_before(status_check); self.save_state()
                                    else: self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state()
                                else:
                                    deploy_ok = best_status == "出战中"
                                    if best_status != "出战中":
                                        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                                        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(best_name, "出战中"); best_status = "出战中"; deploy_ok = True
                                        elif deploy_resp and self.is_fake_beast_status_response(deploy_resp):
                                            if await self.normalize_beast_for_steal(best_name): best_status = "出战中"; deploy_ok = True
                                            else: deploy_ok = False
                                        elif self.handle_beast_deploy_failure_for_steal(best_name, deploy_resp, "偷菜前出战"):
                                            deploy_ok = False
                                        elif deploy_resp:
                                            notify_unrecognized_response(self, f".灵兽出战 {best_name}", deploy_resp, log, "偷菜前出战"); self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); deploy_ok = False
                                        else:
                                            self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); deploy_ok = False
                                        await asyncio.sleep(3)
                                    s_resp = ""
                                    if deploy_ok:
                                        s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
                                    if s_resp and self.is_fake_beast_status_response(s_resp):
                                        if await self.normalize_beast_for_steal(best_name): await asyncio.sleep(3); s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
                                        else: s_resp = ""
                                    if s_resp and self.is_no_beast_deployed_for_steal_response(s_resp):
                                        deploy_resp = await self.send_and_wait_feedback(f".灵兽出战 {best_name}", timeout=60, max_retries=1)
                                        retry_ok = False
                                        if self.is_beast_deploy_success(deploy_resp): self.set_best_beast_status(best_name, "出战中"); best_status = "出战中"; retry_ok = True
                                        elif deploy_resp and self.is_fake_beast_status_response(deploy_resp):
                                            if await self.normalize_beast_for_steal(best_name): best_status = "出战中"; retry_ok = True
                                        elif self.handle_beast_deploy_failure_for_steal(best_name, deploy_resp, "偷菜重试出战"):
                                            s_resp = ""
                                        elif deploy_resp:
                                            notify_unrecognized_response(self, f".灵兽出战 {best_name}", deploy_resp, log, "偷菜重试出战"); self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); s_resp = ""
                                        else:
                                            self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); s_resp = ""
                                        if retry_ok:
                                            await asyncio.sleep(3)
                                            s_resp = await self.send_and_wait_feedback(".灵兽偷菜")
                                        if s_resp and self.is_fake_beast_status_response(s_resp): self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state(); s_resp = ""
                                    if s_resp:
                                        if self.is_beast_pastured_response(s_resp, best_name):
                                            self.defer_beast_actions_while_pastured(best_name, "steal response")
                                            s_resp = ""
                                    if s_resp:
                                        injury_cd = self.record_beast_injury_from_response(best_name, s_resp, source="steal")
                                        if injury_cd >= 0: steal_delay = max(14400, injury_cd); self.state["last_steal_time"] = add_seconds_str(now_str(), steal_delay - 14400); self.state["next_steal_time"] = add_seconds_str(now_str(), steal_delay); self.save_state()
                                        else:
                                            s_cd = self.parse_wait_time(s_resp)
                                            if s_cd > 0: self.state["last_steal_time"] = add_seconds_str(now_str(), s_cd - 14400); self.state["next_steal_time"] = add_seconds_str(now_str(), s_cd)
                                            elif any(k in s_resp for k in ["成功", "获得", "偷菜", "已领命", "潜行"]): self.state["last_steal_time"] = now_str(); self.state["next_steal_time"] = add_seconds_str(now_str(), 14400); self.set_best_beast_status(best_name, "出战中")
                                            else: notify_unrecognized_response(self, ".灵兽偷菜", s_resp, log, "灵兽偷菜"); self.set_next_steal_not_before(add_seconds_str(now_str(), 600))
                                            self.save_state()
                        else:
                            log.warning("Beast action due but cache is empty; scheduling abyss/steal retry.")
                            if need_abyss: await self.execute_abyss_with_fallback()
                            if need_steal: self.set_next_steal_not_before(add_seconds_str(now_str(), 600)); self.save_state()
                    if need_pasture:
                        best_name_for_pasture = self.state.get("best_beast_name", "")
                        best_status_for_pasture = self.state.get("best_beast_status", "")
                        if await self.ensure_focus_beast_deployed_for_pasture():
                            f_resp = await self.send_and_wait_feedback(".一键放养")
                            self.record_auto_pasture_response(f_resp, cache, best_name_for_pasture, best_status_for_pasture)
                        else:
                            log.warning(f"Pasture skipped: {BEAST_FOCUS_NAME} is not confirmed deployed.")
                        await asyncio.sleep(3)
                    if need_interaction:
                        focus = self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
                        focus_status = (focus or {}).get("status", "")
                        if self.is_pastured_status(focus_status):
                            self.defer_beast_actions_while_pastured(BEAST_FOCUS_NAME, "interaction precheck")
                        elif any(k in focus_status for k in ["探险", "偷菜", "巡游"]):
                            retry_time = self.state.get("next_beast_status_check_time", "")
                            retry_seconds = int(seconds_until(retry_time)) if retry_time and is_future(retry_time) else BEAST_ACTION_RETRY_SECONDS
                            log.info(f"Beast interaction deferred: {BEAST_FOCUS_NAME} status is {focus_status}, retry in {retry_seconds}s.")
                            self.schedule_beast_action_retry("next_beast_interaction_time", max(60, retry_seconds))
                        else:
                            interaction_command = self.beast_interaction_command_for_status(focus_status)
                            log.info(f"Beast interaction due: sending {interaction_command} (status={focus_status or '未知'}).")
                            i_resp = await self.send_and_wait_feedback(interaction_command, timeout=60, max_retries=1)
                            self.record_beast_interaction_response(i_resp, interaction_command)
                            self.save_state()
                        await asyncio.sleep(3)
                    if need_cruise:
                        async with AtomicTaskContext(self, "BeastCruise"):
                            if await self.prepare_focus_beast_for_cruise():
                                await self.run_focus_beast_cruise()
                                self.save_state()
                        await asyncio.sleep(3)
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
        remaining = seconds_until(end_time)
        if remaining <= 0: return
        wait_sec = seconds_until(end_time) + random.randint(30, 60)
        if wait_sec > 0: await asyncio.sleep(wait_sec)

    async def run_meditation_timer(self):
        """深度闭关循环：查看状态→结算→重新开始"""
        await self.startup_done.wait()
        while self.is_running:
            retry_time = self.state.get("next_meditation_retry_time", "")
            if retry_time and is_future(retry_time):
                await asyncio.sleep(seconds_until(retry_time) + random.randint(10, 30)); continue
            end_time = self.state.get("deep_meditation_end_time", "")
            if self.state.get("in_deep_meditation") and end_time and is_future(end_time):
                await self.sleep_until_meditation_check(end_time); continue

            async def settle_and_start_deep():
                await self.send_and_wait_feedback(".闭关修炼"); await asyncio.sleep(3)
                med_resp = await self.send_and_wait_feedback(".深度闭关"); cd_med = self.parse_wait_time(med_resp)
                if any(k in med_resp for k in ["冷却", "后再试", "无法立即", "尚未平复"]):
                    if cd_med > 0: self.state["in_deep_meditation"] = False; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), cd_med); return cd_med + random.randint(10, 30)
                if any(k in med_resp for k in ["已进入", "深度闭关", "已在", "开启", "成功"]):
                    self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd_med if cd_med > 0 else 8 * 3600)
                    self.state["in_deep_meditation"] = True; self.state["next_meditation_retry_time"] = ""; self.save_state()
                    await self.place_concubine_after_meditation_start(); return 300
                if med_resp: notify_unrecognized_response(self, ".深度闭关", med_resp, log, "深度闭关")
                self.state["in_deep_meditation"] = False; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600); self.save_state(); return 600

            resp = await self.send_and_wait_feedback(".查看闭关"); cd = self.parse_wait_time(resp); next_sleep = 300
            if cd > 0: self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd); self.state["in_deep_meditation"] = True; self.state["next_meditation_retry_time"] = ""; self.save_state(); await self.sleep_until_meditation_check(self.state["deep_meditation_end_time"]); continue
            elif is_deep_meditation_settlement_response(resp): next_sleep = await settle_and_start_deep()
            elif is_not_deep_meditation_response(resp): next_sleep = await settle_and_start_deep()
            elif is_deep_meditation_ongoing_response(resp): self.state["in_deep_meditation"] = True; self.state["next_meditation_retry_time"] = ""; self.save_state(); await asyncio.sleep(600); continue
            else:
                if resp: notify_unrecognized_response(self, ".查看闭关", resp, log, "闭关状态")
                self.state["in_deep_meditation"] = False; self.state["next_meditation_retry_time"] = add_seconds_str(now_str(), 600); self.save_state(); next_sleep = 600
            self.save_state(); await asyncio.sleep(next_sleep)

    # ---- 身外化身：分身闭关循环 ----

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
                retry_time = a_state.get("next_meditation_retry_time", "")
                if retry_time and is_future(retry_time):
                    await asyncio.sleep(seconds_until(retry_time) + random.randint(10, 30))
                    continue

                # 检查闭关中
                end_time = a_state.get("deep_meditation_end_time", "")
                if a_state.get("in_deep_meditation") and end_time and is_future(end_time):
                    wait_sec = seconds_until(end_time) + random.randint(30, 60)
                    log.info(f"Avatar [{avatar}] in deep meditation until {end_time}. Sleeping {wait_sec}s.")
                    await asyncio.sleep(wait_sec)
                    continue

                # 步骤 1: 查看闭关
                log.info(f"Avatar [{avatar}] meditation check: sending .查看闭关")
                resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关")
                cd = self.parse_wait_time(resp)

                if cd > 0:
                    # 仍在闭关中
                    self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now_str(), cd))
                    self.set_avatar_state(avatar, "in_deep_meditation", True)
                    self.set_avatar_state(avatar, "next_meditation_retry_time", "")
                    await asyncio.sleep(cd + random.randint(30, 60))
                    continue
                elif is_deep_meditation_settlement_response(resp) or is_not_deep_meditation_response(resp):
                    # 需要结算并重新开始
                    next_sleep = await self._avatar_settle_and_start_deep(avatar)
                    await asyncio.sleep(next_sleep)
                    continue
                elif is_deep_meditation_ongoing_response(resp):
                    # 进行中但无法解析剩余时间
                    self.set_avatar_state(avatar, "in_deep_meditation", True)
                    self.set_avatar_state(avatar, "next_meditation_retry_time", "")
                    await asyncio.sleep(600)
                    continue
                else:
                    if resp:
                        log.warning(f"Avatar [{avatar}] unrecognized .查看闭关 response: {resp[:100]}")
                        self.set_avatar_state(avatar, "in_deep_meditation", False)
                        self.set_avatar_state(avatar, "deep_meditation_end_time", "")
                    else:
                        log.warning(f"Avatar [{avatar}] empty response for .查看闭关 (timeout/rate-limited). Preserving state and retrying in 10m.")
                    self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), 600))
                    await asyncio.sleep(600)
                    continue

            except Exception as e:
                log.error(f"Avatar [{avatar}] meditation loop error: {e}")
                await asyncio.sleep(300)

    async def _avatar_settle_and_start_deep(self, avatar):
        """分身闭关结算并重新开始深度闭关。先用 .查看闭关 获取准确状态。"""
        # Step 0: 先发 .查看闭关 获取准确状态
        log.info(f"Avatar [{avatar}] checking meditation status: sending .查看闭关")
        check_resp = await self.send_and_wait_feedback_identity(avatar, ".查看闭关", timeout=30)
        check_text = getattr(check_resp, "text", "") if hasattr(check_resp, "text") else str(check_resp) if check_resp else ""
        if "正在深度闭关" in check_text or "预计还需" in check_text:
            log.info(f"Avatar [{avatar}] .查看闭关: 已在深度闭关中。")
            cd = self.parse_wait_time(check_text)
            if cd > 0:
                self.set_avatar_state(avatar, "in_deep_meditation", True)
                self.set_avatar_state(avatar, "deep_meditation_end_time", add_seconds_str(now_str(), cd))
            return 300
        await asyncio.sleep(3)

        # Step 1: 结算 .闭关修炼
        log.info(f"Avatar [{avatar}] settling meditation: sending .闭关修炼")
        await self.send_and_wait_feedback_identity(avatar, ".闭关修炼")
        await asyncio.sleep(3)

        # Step 2: 开启深度闭关
        log.info(f"Avatar [{avatar}] starting deep meditation: sending .深度闭关")
        med_resp = await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
        cd_med = self.parse_wait_time(med_resp)

        if med_resp and any(k in med_resp for k in ["冷却", "后再试", "无法立即", "尚未平复"]):
            if cd_med > 0:
                self.set_avatar_state(avatar, "in_deep_meditation", False)
                self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), cd_med))
                return cd_med + random.randint(10, 30)

        if med_resp and any(k in med_resp for k in ["已进入", "深度闭关", "已在", "开启", "成功"]):
            end_time = add_seconds_str(now_str(), cd_med if cd_med > 0 else 8 * 3600)
            self.set_avatar_state(avatar, "deep_meditation_end_time", end_time)
            self.set_avatar_state(avatar, "in_deep_meditation", True)
            self.set_avatar_state(avatar, "next_meditation_retry_time", "")
            log.info(f"Avatar [{avatar}] deep meditation started until {end_time}")
            return 300

        if med_resp:
            log.warning(f"Avatar [{avatar}] unrecognized .深度闭关 response: {med_resp[:100]}")
        self.set_avatar_state(avatar, "in_deep_meditation", False)
        self.set_avatar_state(avatar, "next_meditation_retry_time", add_seconds_str(now_str(), 600))
        return 600

    # ---- 身外化身：分身野外历练循环 ----

    async def run_avatar_field_training_loop(self, avatar, initial_delay=0):
        """
        分身野外历练独立循环。
        对指定分身定时发送 .野外历练 谨慎 指令。
        每个分身有独立的冷却状态，存储在 state.avatars[avatar] 中。
        """
        await self.startup_done.wait()
        self._avatar_loop_count += 1
        if initial_delay > 0:
            log.info(f"Avatar [{avatar}] field training loop: waiting {initial_delay}s before start...")
            await asyncio.sleep(initial_delay)

        while self.is_running:
            try:
                a_state = self.get_avatar_state(avatar)
                next_time = a_state.get("next_field_training_time", "")

                if next_time and is_future(next_time):
                    wait_sec = seconds_until(next_time)
                    await asyncio.sleep(min(wait_sec, 600))
                    continue

                # 发送历练指令
                cmd = self.field_training_command  # ".野外历练 谨慎"
                log.info(f"Avatar [{avatar}] field training due: sending {cmd}")
                resp = await self.send_and_wait_feedback_identity(avatar, cmd, timeout=90)

                # 解析回复并更新分身独立冷却
                now = now_str()
                if not resp:
                    self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now, 600))
                    log.warning(f"Avatar [{avatar}] field training: no response; retry in 10min.")
                else:
                    cd = self.parse_wait_time(resp)
                    clean = (resp or "").replace("**", "")
                    is_cooldown = any(k in clean for k in ["山中灵机未复", "冷却", "后再", "尚未", "请在"])
                    is_success = "野外历练" in clean or "山中灵机未复" in clean or ("卦象" in clean and "修为增加" in clean)

                    if is_cooldown and cd > 0:
                        self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now, cd))
                        log.info(f"Avatar [{avatar}] field training cooldown: {cd}s")
                    elif is_success:
                        self.set_avatar_state(avatar, "last_field_training_time", now)
                        self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now, 7200))
                        log.info(f"Avatar [{avatar}] field training recorded. Next in 2h.")
                    else:
                        self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now, 600))
                        log.warning(f"Avatar [{avatar}] field training unrecognized: {resp[:100]}")

                await asyncio.sleep(5)

            except Exception as e:
                log.error(f"Avatar [{avatar}] field training loop error: {e}")
                await asyncio.sleep(300)

    # ---- 身外化身：分身闯塔循环 ----

    async def run_avatar_tower_loop(self, avatar, initial_delay=0):
        """
        分身每日 23 点自动闯塔任务。
        每天 23:00 - 23:30 之间随机错开时间，为指定分身发送一次 .闯塔。
        状态记录在 state.avatars[avatar]["last_tower_date"] 中。
        """
        await self.startup_done.wait()
        self._avatar_loop_count += 1
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        while self.is_running:
            try:
                now = datetime.now()
                today = now.strftime('%Y-%m-%d')
                a_state = self.get_avatar_state(avatar)
                last_date = a_state.get("last_tower_date", "")

                # 如果今天还没闯塔，且当前处于 23 点
                if last_date != today and now.hour == 23:
                    # 错开 10 - 600 秒（10分钟）内的一个随机时间，避免三个化身扎堆发送
                    delay = random.randint(10, 600)
                    log.info(f"Avatar [{avatar}] daily tower due today ({today}). Waiting {delay}s to staggered start...")
                    await asyncio.sleep(delay)

                    # 再次确认日期没有在休眠期间被其他协程更新
                    a_state = self.get_avatar_state(avatar)
                    if a_state.get("last_tower_date", "") != today:
                        log.info(f"Avatar [{avatar}] daily tower start: sending .闯塔")
                        resp = await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=90)

                        if resp is None:
                            # 切换失败或发送失败，不标记完成，5分钟后重试
                            log.warning(f"Avatar [{avatar}] daily tower: switch/send failed (resp=None). Retrying in 5 min.")
                            await asyncio.sleep(300)
                            continue

                        # 只要有回复（即使是提示次数不足），都标记为今天已做过，防止无限重试
                        self.set_avatar_state(avatar, "last_tower_date", today)
                        log.info(f"Avatar [{avatar}] daily tower recorded done for today.")

                # 没到时间或者已完成，每 10 分钟检测一次
                await asyncio.sleep(600)

            except Exception as e:
                log.error(f"Avatar [{avatar}] tower loop error: {e}")
                await asyncio.sleep(300)

    # ============================================================
    # 化身日常与星宫相关循环
    # ============================================================

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

    @safe_bg_task
    async def delayed_avatar_force_exit(self, avatar, delay_sec):
        try:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            log.info(f"Avatar {avatar}: force-exit timer elapsed. Forcing exit.")
            async with self.avatar_send_lock:
                # 独占物理管线期间的身份对齐
                if self.current_identity != avatar:
                    switch_target = "主魂" if avatar == "主魂" else avatar
                    switch_cmd = f".切换 {switch_target}"
                    log.info(f"🔄 Force Exit Exclusive Switch: {self.current_identity} → {avatar} (sending {switch_cmd})")
                    switch_resp = await self._send_and_wait_feedback_raw(switch_cmd, timeout=30, max_retries=2)
                    if switch_resp and ("成功" in switch_resp or "已切换" in switch_resp or "当前操控" in switch_resp or avatar in switch_resp):
                        self.current_identity = avatar
                        self._main_confirmed = False  # 化身已切换，主魂确认失效
                        log.info(f"✅ Force Exit Switch confirmed: now {avatar}")
                    else:
                        self.current_identity = avatar
                        self._main_confirmed = False  # 化身已切换，主魂确认失效
                    await asyncio.sleep(2)

                await self._send_and_wait_feedback_raw(".强行出关")
                await asyncio.sleep(3)
                await self._send_and_wait_feedback_raw(".深度闭关")
            self.set_avatar_state(avatar, "next_force_exit_time", "")
        except Exception as e:
            log.error(f"Avatar {avatar} delayed_force_exit error: {e}")

    def heart_trial_anchor_lost(self, text):
        clean = str(text or "").replace("**", "")
        return "心劫锚点已散" in clean or "需重新引动天劫" in clean

    async def execute_avatar_heart_trial_flow(self, avatar, send_with_cultivation_check):
        """
        化身共历心劫原子流程：
        .我的侍妾 -> .共历心劫 -> .稳 x3 必须连续执行，避免身份切换打散回复锚点。
        """
        async with AtomicTaskContext(self, f"HeartTrial-{avatar}"):
            forced_exit = False
            for flow_attempt in range(1, 3):
                status_msg = await self.send_and_wait_feedback_identity(
                    avatar, ".我的侍妾", return_response_msg=True, delete_after=False
                )
                status_text = getattr(status_msg, "text", "") if hasattr(status_msg, "text") else str(status_msg) if isinstance(status_msg, str) else ""
                if status_text and "还没有侍妾" in status_text:
                    log.info(f"Avatar {avatar} has no concubine. Disabling heart trial for 24 hours.")
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 24 * 3600))
                    return

                if status_text:
                    voyage_block_until = self.parse_concubine_voyage_status_line(status_text, avatar)
                    if voyage_block_until:
                        self.set_avatar_state(avatar, "next_heart_trial_time", voyage_block_until)
                        log.info(f"Avatar {avatar} heart trial blocked by active voyage until {voyage_block_until}.")
                        return
                    clean_status = status_text.replace("**", "")
                    match = re.search(r"(?:共历)?心劫冷却\s*[：:]\s*([^\s\n|]+)", clean_status)
                    if match:
                        val = match.group(1).strip()
                        if not any(k in val for k in ["无", "可用", "可施展", "已就绪"]):
                            cd = getattr(self, "parse_wait_time", lambda x: 0)(val)
                            if cd > 0:
                                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), cd + 60))
                                log.info(f"Avatar {avatar} parsed heart trial CD from status: {cd}s.")
                                return

                if not (status_msg and hasattr(status_msg, "id")):
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                    return

                resp_msg, step_forced_exit = await send_with_cultivation_check(
                    ".共历心劫", reply_to=status_msg.id, return_response_msg=True
                )
                forced_exit = forced_exit or step_forced_exit
                if resp_msg == "PAUSE_1H":
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 3600))
                    break

                resp_str = getattr(resp_msg, "text", "") if hasattr(resp_msg, "text") else str(resp_msg) if isinstance(resp_msg, str) else ""
                if resp_str and "冷却" in resp_str:
                    cd = self.parse_wait_time(resp_str)
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), cd if cd > 0 else 1800))
                    break
                if self.concubine_response_indicates_active_voyage(resp_str):
                    block_until = self.concubine_voyage_block_until(avatar) or add_seconds_str(now_str(), 1800)
                    self.set_avatar_state(avatar, "next_heart_trial_time", block_until)
                    log.info(f"Avatar {avatar} heart trial blocked by active voyage until {block_until}.")
                    break
                if self.heart_trial_requires_reply_target(resp_str):
                    log.warning(f"Avatar {avatar} 共历心劫: bot still requires reply target (attempt {flow_attempt}/2).")
                    if flow_attempt < 2:
                        await asyncio.sleep(3)
                        continue
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                    break
                if not (resp_str and "第一轮" in resp_str):
                    log.warning(f"Avatar {avatar} 共历心劫: response did not start round 1: {resp_str[:120]}")
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                    break

                current_msg = resp_msg
                trial_failed = False
                anchor_lost = False
                async with self.avatar_send_lock:
                    for idx in range(1, 4):
                        confirmed = False
                        current_text = getattr(current_msg, "text", "") if hasattr(current_msg, "text") else ""
                        for attempt in range(1, 4):
                            try:
                                await self.pause_event.wait()
                                if not await wait_for_bot_activity_before_send(self, ".稳", log):
                                    trial_failed = True
                                    break
                                if not command_send_allowed(self, ".稳", log):
                                    trial_failed = True
                                    break
                                remember_script_send_intent(self, ".稳")
                                sent = await self.client.send_message(self.target_chat_id, ".稳", reply_to=current_msg.id)
                                remember_script_sent_message(self, sent)
                                schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                                log.info(f"🟢 OUT [{avatar}]:\n.稳 ({idx}/3, try {attempt}/3)")

                                result_msg, current_text, confirmed = await self.wait_for_heart_trial_round_result(
                                    current_msg, sent, idx, timeout_sec=90, poll_sec=3,
                                )
                                if result_msg:
                                    current_msg = result_msg
                                    await log_incoming_message(self, f".稳 {idx}/3 try {attempt}/3 ({avatar})", current_text, msg=result_msg, logger=log)

                                if self.heart_trial_anchor_lost(current_text):
                                    log.warning(f"Avatar {avatar} 共历心劫: anchor lost at round {idx}, will re-init trial.")
                                    anchor_lost = True
                                    trial_failed = True
                                    break
                                if self.heart_trial_settled(current_text):
                                    confirmed = True
                                    break
                                if self.heart_trial_round_confirmed(current_text, idx):
                                    confirmed = True
                                    break
                                if self.heart_trial_round_prompt(current_text, idx) and attempt < 3:
                                    await asyncio.sleep(3)
                                    continue
                            except Exception as e:
                                log.error(f"Avatar {avatar} 共历心劫: failed to send .稳 ({idx}/3): {e}")
                                trial_failed = True
                                break

                            if not confirmed:
                                break

                        if trial_failed:
                            break
                        if self.heart_trial_settled(current_text):
                            break
                        if not confirmed:
                            log.warning(f"Avatar {avatar} 共历心劫: round {idx} did not confirm.")
                            trial_failed = True
                            break

                if anchor_lost and flow_attempt < 2:
                    await asyncio.sleep(3)
                    continue
                if trial_failed:
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 600))
                else:
                    self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 10 * 3600))
                break

            if forced_exit:
                await self.send_and_wait_feedback_identity(avatar, ".深度闭关")

    async def run_avatar_star_palace_loop(self, avatar, initial_delay=0):
        """化身专属星宫循环（仅限素心子、缘生子）"""
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
                is_star_palace = avatar in ["素心子", "缘生子"]

                # --- 宗门点卯（每日一次，07:30 后） ---
                await self._avatar_daily_checkin(avatar)
                state = self.get_avatar_state(avatar)
                
                # --- 星辰牵引/安抚/收集由 run_avatar_star_attraction_loop 独立调度 ---

                # --- 周天星斗大阵 (12小时冷却) ---
                last_formation = state.get("last_formation_time", "")
                next_formation = state.get("next_formation_time", "")
                if is_star_palace and (not next_formation or not is_future(next_formation)):
                    resp, forced_exit = await send_with_cultivation_check(".启阵")
                    # 启阵成功后，通知配对化身助阵
                    if resp and "周天星斗大阵" in str(resp) and "尚需" in str(resp):
                        asyncio.create_task(self.mutual_formation_assist(avatar, resp.id if hasattr(resp, "id") else 0))
                    resp_str = str(resp) if resp else ""
                    if resp == "PAUSE_1H":
                        self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 3600))
                    elif "周天星斗大阵" in resp_str and "尚需" in resp_str:
                        log.info(f"Avatar {avatar} formation pending assist, waiting 65s to check result...")
                        await asyncio.sleep(65)
                        if hasattr(resp, "id"):
                            updated_msg = await self.client.get_messages(self.target_chat_id, ids=resp.id)
                            resp_str = updated_msg.text or ""
                        if "大阵已成" in resp_str or "阵成" in resp_str:
                            self.set_avatar_state(avatar, "last_formation_time", now_str())
                            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 12 * 3600))
                            self.set_avatar_state(avatar, "formation_active_until", add_seconds_str(now_str(), 6 * 3600))
                            self.set_avatar_state(avatar, "next_formation_retry_time", "")  # 清空重试时间
                            force_delay = 5 * 3600 + 55 * 60
                            self.set_avatar_state(avatar, "next_force_exit_time", add_seconds_str(now_str(), force_delay))
                            asyncio.create_task(self.delayed_avatar_force_exit(avatar, force_delay))
                            log.info(f"Avatar {avatar} formation success! Force exit in 5h55m.")
                        else:
                            log.info(f"Avatar {avatar} formation failed (no assist). Retrying in 10m.")
                            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 600))
                    elif "大阵已成" in resp_str or "阵成" in resp_str:
                        self.set_avatar_state(avatar, "last_formation_time", now_str())
                        self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 12 * 3600))
                        self.set_avatar_state(avatar, "formation_active_until", add_seconds_str(now_str(), 6 * 3600))
                        self.set_avatar_state(avatar, "next_formation_retry_time", "")  # 清空重试时间
                        force_delay = 5 * 3600 + 55 * 60
                        self.set_avatar_state(avatar, "next_force_exit_time", add_seconds_str(now_str(), force_delay))
                        asyncio.create_task(self.delayed_avatar_force_exit(avatar, force_delay))
                        log.info(f"Avatar {avatar} formation success immediately! Force exit in 5h55m.")
                    elif "冷却" in resp_str or "心神消耗" in resp_str or "再次启阵" in resp_str:
                        cd = getattr(self, "parse_wait_time", lambda x: -1)(resp_str)
                        if cd > 0:
                            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), cd))
                        else:
                            self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 1800))
                    else:
                        self.set_avatar_state(avatar, "next_formation_time", add_seconds_str(now_str(), 600))
                    
                    if forced_exit:
                        await self.send_and_wait_feedback_identity(avatar, ".深度闭关")

                # --- 入梦寻图 / 星宫道心侍妾远航绑定批次 ---
                handled_bound_batch = await self.execute_avatar_bound_dream_voyage(
                    avatar,
                    send_with_cultivation_check=send_with_cultivation_check,
                )
                if not handled_bound_batch:
                    next_dream = state.get("next_dream_map_time", "")
                    if not next_dream or not is_future(next_dream):
                        resp, forced_exit = await send_with_cultivation_check(".入梦寻图")
                        if resp == "PAUSE_1H":
                            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 3600))
                        elif resp:
                            resp_str = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
                            if resp_str and any(k in resp_str for k in ["未拥有", "不足"]):
                                log.info(f"Avatar {avatar} has no dream map fragments, pausing for 24h.")
                                self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 24 * 3600))
                            elif resp_str and "冷却" in resp_str:
                                cd = self.parse_wait_time(resp_str)
                                cd_seconds = cd if cd > 0 else 1800
                                self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), cd_seconds))
                            else:
                                self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 8 * 3600))
                                self.mark_concubine_dream_executed(avatar)
                                # 入梦寻图进度 4/4 时自动发送 .拼图
                                if resp_str and "4/4" in resp_str:
                                    log.info(f"Avatar {avatar} dream map progress 4/4, sending .拼图")
                                    await asyncio.sleep(3)
                                    await self.send_and_wait_feedback_identity(avatar, ".拼图")
                        else:
                            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 600))
                        if forced_exit:
                            await self.send_and_wait_feedback_identity(avatar, ".深度闭关")

                    # --- 侍妾远航（非绑定路径兜底） ---
                    await self.execute_avatar_concubine_voyage(avatar)

                # --- 共历心劫 (10小时冷却) ---
                next_heart = state.get("next_heart_trial_time", "")
                if not next_heart or not is_future(next_heart):
                    await self.execute_avatar_heart_trial_flow(avatar, send_with_cultivation_check)

            except Exception as e:
                log.error(f"Error in avatar {avatar} star palace loop: {e}", exc_info=True)
                # M3: 如果已强行出关但异常中断，恢复深度闭关避免化身空转
                if forced_exit:
                    try:
                        await self.send_and_wait_feedback_identity(avatar, ".深度闭关")
                        log.info(f"Avatar {avatar}: restored deep meditation after exception.")
                    except Exception as e2:
                        log.error(f"Avatar {avatar}: failed to restore deep meditation: {e2}")
            
            await asyncio.sleep(300)

    # ---- 启动 ----

    async def start(self):
        """脚本入口：连接 Telegram、注册事件处理器、启动所有循环"""
        await self.client.start()
        await self.client.get_dialogs(limit=10)
        self.my_info = await self.client.get_me()
        log.info(f"XiaoHao Login: {self.my_info.first_name}")
        @self.client.on(events.NewMessage(chats=self.target_chat_id))
        async def h(e): await self.handle_game_response(e)
        @self.client.on(events.MessageEdited(chats=self.target_chat_id))
        async def eh(e):
            await log_edited_message_if_needed(self, e)
            try:
                msg = e.message
                text = msg.text or ""
                sender = await e.get_sender()
                if is_game_bot_sender(self, sender):
                    record_game_bot_activity(self, sender, log)
                    # 编辑后出现元婴遁逃·虚弱 → 立刻告警并停止脚本（防漏检补丁，加入账号强匹配）
                    if self.is_rift_weakness_response(text) and is_edited_message_for_current_account(self, msg, text):
                        log.critical(f"Rift weakness DETECTED in edited message! Stopping immediately.\n{text}")
                        await self.stop_for_rift_weakness(text)
                        return
                    await record_manual_command_reply_state_if_needed(self, msg, text, sender, log)
                    self.maybe_record_avatar_passive_states(msg)
                    # 编辑消息也能触发 feedback_events（bot 通过编辑回复指令，如共历心劫）
                    is_matched = False
                    # 1. 回复匹配：编辑的消息是某条待处理指令的回复目标
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
                                if cmd_text == ".查看闭关" and not self.is_loose_meditation_feedback_candidate(cmd_text, text):
                                    log.warning(f"[EDITED-FEEDBACK] Rejected [{cmd_text}]: content unrelated (msg {msg.id}): {text[:80]}")
                                    _skip_reply = True
                                if not _skip_reply:
                                    self.last_feedback_text[replied_id] = text
                                    self.last_feedback_msg[replied_id] = msg
                                    evt.set()
                                    is_matched = True
                                    log.info(f"[EDITED-FEEDBACK] Matched by reply_to={replied_id}, triggered feedback event.")
                    # 2. 消息ID匹配：编辑的消息本身就是待处理指令的响应目标
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
                                log.info(f"[EDITED-FEEDBACK] Matched by msg_id={msg.id}, triggered feedback event.")
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
                        )
            except Exception as ex:
                log.error(f"Edited message handler error: {ex}")
            await self.handle_pasture_return_event(e)

        async def startup_sync():
            """启动后对账：同步狩猎/探渊/闭关状态，避免重启后丢失进度"""
            await asyncio.sleep(25)
            log.info("Startup Sync: Smart check for stale data...")
            
            # 启动时保留上次记录的身份；真正需要主魂命令时由 send_and_wait_feedback 对齐。
            self._main_confirmed = self.current_identity == "主魂"
            log.info(
                "Startup Sync: Keep persisted identity "
                f"{self.current_identity}; main_confirmed={self._main_confirmed}."
            )
            await asyncio.sleep(3)
            
            async with self.beast_lock:
                focus = self.get_cached_beast_by_name(BEAST_FOCUS_NAME)
                focus_status = (focus or {}).get("status", "") or self.state.get("best_beast_status", "")
                focus_name = (focus or {}).get("full_name") or self.state.get("best_beast_name", "") or BEAST_FOCUS_NAME
                if self.is_pastured_status(focus_status):
                    self.defer_beast_actions_while_pastured(focus_name, "startup persisted pastured status")
                if self.state.get("beast_hunt_stopped"): log.info(f"Startup Sync: Hunt stopped ({self.state.get('beast_hunt_stopped_reason', '')}).")
                elif self.should_stop_hunt_by_tenth_beast(self.state.get("beasts_cache", [])): log.info("Startup Sync: Hunt stopped by cached 10th beast.")
                else:
                    next_hunt = self.state.get("next_hunt_time", "")
                    if next_hunt and is_future(next_hunt): log.info(f"Startup Sync: Hunt deferred until {next_hunt}.")
                    else:
                        last_hunt = self.state.get("last_hunt_time", "")
                        if last_hunt and is_future(add_seconds_str(last_hunt, HUNT_CD_SECONDS)): log.info(f"Startup Sync: Local hunt time valid. Skipping.")
                        else:
                            resp = await self.send_and_wait_feedback(".寻觅灵兽")
                            if resp:
                                cd = self.parse_wait_time(resp)
                                if self.is_beast_bag_full_response(resp): self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FULL_RETRY_SECONDS)
                                elif cd > 0: self.state["last_hunt_time"] = add_seconds_str(now_str(), cd - HUNT_CD_SECONDS); self.state["next_hunt_time"] = add_seconds_str(now_str(), cd)
                                elif any(k in resp for k in ["成功", "出发", "抓到", "寻觅", "搜寻"]): self.state["last_hunt_time"] = now_str(); self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_CD_SECONDS)
                                else: self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS)
                            else: self.state["next_hunt_time"] = add_seconds_str(now_str(), HUNT_FAIL_RETRY_SECONDS)
                last_abyss = self.state.get("last_abyss_time", "")
                if last_abyss:
                    self.state["next_abyss_time"] = add_seconds_str(last_abyss, 6*3600); self.save_state()
                    if is_future(self.state["next_abyss_time"]): log.info(f"Startup Sync: Abyss in progress, next at {self.state['next_abyss_time']}.")
                    else:
                        await self.execute_abyss_with_fallback()
            end_med = self.state.get("deep_meditation_end_time", "")
            if end_med and is_future(end_med): log.info(f"Startup Sync: Local meditation time valid. Skipping.")
            else:
                resp_med = await self.send_and_wait_feedback(".查看闭关")
                if resp_med:
                    cd_med = self.parse_wait_time(resp_med)
                    if cd_med > 0: self.state["deep_meditation_end_time"] = add_seconds_str(now_str(), cd_med); self.state["in_deep_meditation"] = True
                    elif is_deep_meditation_settlement_response(resp_med) or is_not_deep_meditation_response(resp_med): self.state["in_deep_meditation"] = False; self.state["deep_meditation_end_time"] = ""
            self.save_state(); self.startup_done.set(); log.info("Startup Sync: Finished. All loops released.")
        asyncio.create_task(startup_sync())
        asyncio.create_task(periodic_log_prune(LOG_FILE))

        # 启动所有定时任务
        asyncio.create_task(self.run_daily_tasks())
        asyncio.create_task(self.run_beast_hunt_timer())
        asyncio.create_task(self.run_beast_action_timer())
        asyncio.create_task(self.run_meditation_timer())
        asyncio.create_task(self.run_concubine_loop())
        asyncio.create_task(self.run_field_training_loop())
        asyncio.create_task(self.run_sect_war_loop())
        asyncio.create_task(self.run_custom_command_loop())
        asyncio.create_task(self.run_treasure_touch_loop())
        asyncio.create_task(self.run_yuanying_out_loop())
        asyncio.create_task(self.run_rift_search_loop())

        # 身外化身：为每个分身启动独立的闭关+历练+闯塔循环（取消强制错开等待，完全依赖全局锁排队执行）
        for avatar in self.avatars:
            asyncio.create_task(self.run_avatar_meditation_loop(avatar, initial_delay=0))
            asyncio.create_task(self.run_avatar_field_training_loop(avatar, initial_delay=0))
            asyncio.create_task(self.run_avatar_tower_loop(avatar, initial_delay=0))
            # 所有分身都启动此循环，内含对星宫指令的身份判定，问心子借此执行入梦和心劫
            asyncio.create_task(self.run_avatar_star_palace_loop(avatar, initial_delay=0))
            if avatar in STAR_ATTRACTION_AVATARS:
                asyncio.create_task(self.run_avatar_star_attraction_loop(avatar, initial_delay=0))
            if avatar == "问心子":
                asyncio.create_task(self.run_avatar_cloud_stairs_loop(avatar, initial_delay=0))
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
