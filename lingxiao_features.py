"""Identity-scoped Lingxiao stairs, wind and daily heart-platform workflow."""
import asyncio
from datetime import datetime, timedelta
import logging
import random
import re

from log_utils import notify_unrecognized_response
from sect_rules import SectTaskStopped, identity_sect, task_paused

log = logging.getLogger("Lingxiao")
CLOUD_STAIRS_CD_SECONDS = 3 * 3600
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_str():
    return datetime.now().strftime(TIME_FORMAT)


def str_to_dt(value):
    return datetime.strptime(value, TIME_FORMAT)


def dt_to_str(value):
    return value.strftime(TIME_FORMAT)


def add_seconds_str(value, seconds):
    return dt_to_str(str_to_dt(value) + timedelta(seconds=seconds))


def is_future(value):
    return bool(value and isinstance(value, str) and str_to_dt(value) > datetime.now())


def seconds_until(value):
    return max(0, (str_to_dt(value) - datetime.now()).total_seconds())


class LingxiaoMixin:
    def lingxiao_state(self, identity):
        return self.identity_state_for_timed_command(self.resolve_avatar_identity(identity))

    def set_lingxiao_state(self, identity, key, value):
        self.lingxiao_state(identity)[key] = value

    def record_lingxiao_weeks(self, identity, text, source="Lingxiao"):
        match = re.search(r"(?:已完成周天[：:]\s*|完成了第\s*)(\d+)\s*轮", str(text or "").replace("**", ""))
        if match:
            self.lingxiao_state(identity)["completed_weeks"] = match.group(1) + " 轮"
        return bool(match)

    async def send_lingxiao_command(self, identity, command, **kwargs):
        identity = self.resolve_avatar_identity(identity)
        if not self.sect_operation_allowed(identity, command):
            raise SectTaskStopped(command)
        if identity == "主魂":
            response = await self.send_and_wait_feedback(command, **kwargs)
        else:
            response = await self.send_and_wait_feedback_identity(identity, command, **kwargs)
        return self.common_response_text(response)

    def lingxiao_step(self, avatar):
        """
        从缓存的化身状态中获取当前云阶步数。
        格式如 "5 / 12 阶"，提取数字部分。
        """
        current_progress = self.lingxiao_state(avatar).get("cloud_stairs_progress", "0 / 12")
        try:
            return int(re.search(r'(\d+)', current_progress).group(1))
        except Exception:
            return 0

    def lingxiao_unavailable(self, text):
        clean = str(text or "").replace("**", "")
        return "你并非凌霄宫弟子" in clean or "云阶禁制不会为你显现" in clean

    def lingxiao_mismatch(self, avatar, response_text="", source="Cloud stairs"):
        state = self.lingxiao_state(avatar)
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

    def lingxiao_record_stairs(self, avatar, stairs_resp, source="Cloud stairs climb"):
        if not stairs_resp:
            return False

        if self.lingxiao_unavailable(stairs_resp):
            self.lingxiao_mismatch(avatar, stairs_resp, source=source)
            return False

        # ---- 登阶成功 ----
        if (
            "【凌霄云阶】" in stairs_resp
            or ("踏上" in stairs_resp and "云阶" in stairs_resp)
            or ("当前云阶进度" in stairs_resp and any(k in stairs_resp for k in ["本次获得", "额外收获", "修为", "宗门贡献", "罡风淬体"]))
        ):
            self.lingxiao_record_progress(avatar, stairs_resp, source=source)
            self.record_lingxiao_weeks(avatar, stairs_resp, source=source)

            now = now_str()
            self.set_lingxiao_state(avatar, "last_stairs_time", now)
            self.set_lingxiao_state(avatar, "last_stairs_success_time", now)
            self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now, 3 * 3600))
            self.record_daily_reward_event(avatar, ".登天阶", stairs_resp, source=source)
            log.info(f"Cloud stairs [{avatar}] success, next run at {self.lingxiao_state(avatar).get('next_stairs_time', '')}")
            return True

        # ---- 冷却中 ----
        cd = self.parse_wait_time(stairs_resp, line_identifier="登阶冷却")
        if cd <= 0 and any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            cd = self.parse_wait_time(stairs_resp)

        if cd > 0:
            self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now_str(), cd))
            log.info(f"Cloud stairs [{avatar}] CD from response: {cd}s, next run at {self.lingxiao_state(avatar).get('next_stairs_time', '')}")
            return False

        # ---- 不可用但无 CD 信息 ----
        if any(k in stairs_resp for k in ["请在", "后再", "冷却", "尚未", "未再聚"]):
            log.warning(f"Cloud stairs [{avatar}] appears unavailable but no CD parsed: {stairs_resp[:100]}")
            notify_unrecognized_response(self, ".登天阶", stairs_resp, log, "登天阶冷却解析")
            self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 600))
            self.save_state()
            return False

        # ---- 完全无法识别的回复 ----
        notify_unrecognized_response(self, ".登天阶", stairs_resp, log, source)
        self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 600))
        self.save_state()
        return False

    def lingxiao_record_progress(self, avatar, text: str, source="Cloud stairs"):
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
        self.record_lingxiao_weeks(avatar, text, source=source)

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
            existing_total = re.search(r'/\s*(\d+)', self.lingxiao_state(avatar).get("cloud_stairs_progress", ""))
            total = existing_total.group(1) if existing_total else "12"

        self.set_lingxiao_state(avatar, "cloud_stairs_progress", f"{current} / {total} 阶")
        log.info(f"{source}: Cloud stairs progress synced to {current}/{total}")
        return True

    def lingxiao_restore_stairs_time(self, avatar):
        """
        从上次成功时间推断下次可登天阶时间。
        如果 next_stairs_time 为空但 last_stairs_time 存在，
        用 last_stairs_time + 3小时 推断 next_stairs_time。

        这样即使状态文件丢失了 next_stairs_time，也能恢复 CD 信息。

        返回:
            str: next_stairs_time（原始或推断的）
        """
        last_stairs = self.lingxiao_state(avatar).get("last_stairs_time", "")
        next_stairs = self.lingxiao_state(avatar).get("next_stairs_time", "")
        if next_stairs:
            return next_stairs
        if last_stairs:
            inferred = add_seconds_str(last_stairs, CLOUD_STAIRS_CD_SECONDS)
            if is_future(inferred):
                self.set_lingxiao_state(avatar, "next_stairs_time", inferred)
                self.save_state()
                log.info(f"Cloud stairs next restored from last success: {inferred}")
                return inferred
        return next_stairs

    def lingxiao_heart_fallback_time(self, today):
        return f"{today} 23:50:00"

    def lingxiao_heart_fallback_due(self, today):
        return datetime.now() >= str_to_dt(self.lingxiao_heart_fallback_time(today))

    def lingxiao_heart_throttled(self, avatar):
        last_heart = self.lingxiao_state(avatar).get("last_heart_time", "")
        if not last_heart:
            return False
        elapsed = (datetime.now() - str_to_dt(last_heart)).total_seconds()
        if elapsed < 3 * 3600:
            next_allowed = add_seconds_str(last_heart, 3 * 3600)
            self.set_lingxiao_state(avatar, "next_heart_time", next_allowed)
            log.info(f"Heart Platform [{avatar}] skipped: last use at {last_heart}, next allowed at {next_allowed}.")
            self.save_state()
            return True
        return False

    def lingxiao_heart_used_today(self, avatar, today):
        if self.lingxiao_state(avatar).get("heart_platform_date") == today:
            return True
        last_heart = self.lingxiao_state(avatar).get("last_heart_time", "")
        if last_heart.startswith(today):
            self.set_lingxiao_state(avatar, "heart_platform_date", today)
            self.set_lingxiao_state(avatar, "next_heart_time", add_seconds_str(f"{today} 00:05:00", 24 * 3600))
            self.save_state()
            log.info(f"Heart Platform [{avatar}] skipped: last use already recorded today at {last_heart}.")
            return True
        return False

    def lingxiao_pending_wind(self, avatar):
        last_wind = self.lingxiao_state(avatar).get("last_wind_time") or self.lingxiao_state(avatar).get("last_wind_success_time", "")
        last_stairs = self.lingxiao_state(avatar).get("last_stairs_time") or self.lingxiao_state(avatar).get("last_stairs_success_time", "")
        return bool(last_wind and (not last_stairs or last_wind > last_stairs))

    def lingxiao_wind_ready(self, avatar):
        wind_cd_time = self.lingxiao_state(avatar).get("nine_heaven_wind_cd_time", 0)
        return not wind_cd_time or (isinstance(wind_cd_time, str) and not is_future(wind_cd_time))

    def lingxiao_record_wind(self, avatar, wind_resp, source="Nine Heaven Wind"):
        if not wind_resp:
            return False

        if self.lingxiao_unavailable(wind_resp):
            self.lingxiao_mismatch(avatar, wind_resp, source=source)
            return False

        cd = self.parse_wait_time(wind_resp, line_identifier="引九天罡风")
        if cd <= 0:
            cd = self.parse_wait_time(wind_resp, line_identifier="罡风")
        if cd <= 0 and any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            cd = self.parse_wait_time(wind_resp)

        if cd > 0:
            now = datetime.now()
            self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", dt_to_str(now + timedelta(seconds=cd)))
            if cd <= 12 * 3600:
                inferred_success = now + timedelta(seconds=cd) - timedelta(seconds=12 * 3600)
                current_last = self.lingxiao_state(avatar).get("last_wind_time") or self.lingxiao_state(avatar).get("last_wind_success_time", "")
                if not current_last or inferred_success > str_to_dt(current_last):
                    inferred_str = dt_to_str(inferred_success)
                    self.set_lingxiao_state(avatar, "last_wind_time", inferred_str)
                    self.set_lingxiao_state(avatar, "last_wind_success_time", inferred_str)
                    log.info(f"{source} [{avatar}]: Wind last success inferred from CD as {inferred_str}")
            log.info(f"{source} [{avatar}]: Wind CD from response: {cd}s")
            self.save_state()
            return False

        if any(k in wind_resp for k in ["尚未", "后再", "冷却", "未再聚"]):
            log.warning(f"{source} [{avatar}]: Wind appears unavailable but no CD parsed: {wind_resp[:100]}")
            notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, f"{source} 冷却解析")
            self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), 600))
            self.save_state()
            return False

        if any(k in wind_resp for k in ["成功", "施展", "罡风", "淬体"]):
            cd_seconds = 12 * 3600
            now = now_str()
            self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now, cd_seconds))
            self.set_lingxiao_state(avatar, "last_wind_time", now)
            self.set_lingxiao_state(avatar, "last_wind_success_time", now)
            log.info(f"{source} [{avatar}]: Wind success, next run in {cd_seconds}s")
            self.save_state()
            return True

        log.warning(f"{source} [{avatar}]: Wind response unusual: {wind_resp[:100]}")
        notify_unrecognized_response(self, ".引九天罡风", wind_resp, log, source)
        self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), 600))
        self.save_state()
        return False

    async def lingxiao_use_wind(self, avatar, source="Nine Heaven Wind"):
        if not self.sect_operation_allowed(avatar, ".引九天罡风"):
            return False
        if self.lingxiao_pending_wind(avatar):
            log.info(f"{source} [{avatar}]: pending Wind buff already exists; skip .引九天罡风.")
            return True
        if not self.lingxiao_wind_ready(avatar):
            return False

        log.info(f"{source} [{avatar}]: Wind is ready. Sending .引九天罡风.")
        wind_resp = await self.send_lingxiao_command(
            avatar, ".引九天罡风", timeout=120, force_identity_check=True
        )
        wind_pending = self.lingxiao_record_wind(avatar, wind_resp, source=source)
        await asyncio.sleep(3)
        return wind_pending

    async def lingxiao_use_heart(self, avatar, curr_step, today, allow_daily_fallback=False):
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
        if not self.sect_operation_allowed(avatar, ".问心台"):
            return False
        late_fallback = allow_daily_fallback and self.lingxiao_heart_fallback_due(today)
        # 非高阶（8-11）且非保底时间，跳过
        if not (8 <= curr_step <= 11) and not late_fallback:
            return False
        # 冷却中，跳过
        if self.lingxiao_heart_throttled(avatar):
            return False
        # 今天已使用，跳过
        if self.lingxiao_heart_used_today(avatar, today):
            return False

        # 罡风 buff 还在的话，问心台让路
        wind_pending = self.lingxiao_pending_wind(avatar)
        if wind_pending:
            log.info(f"Heart Platform [{avatar}] skipped: pending Wind buff has priority.")
            return False

        # 如果罡风冷却完毕但还没用，优先用罡风
        if self.sect_operation_allowed(avatar, ".引九天罡风") and self.lingxiao_wind_ready(avatar):
            log.info(f"Heart Platform check at {curr_step}/12 for [{avatar}], but Wind is ready. Sending .引九天罡风 first; Heart Platform is skipped.")
            wind_pending = await self.lingxiao_use_wind(avatar, source="Cloud Stairs")

            # 如果罡风用了或者还是冷却完毕状态（说明施展失败），跳过问心台
            if wind_pending or self.lingxiao_wind_ready(avatar):
                log.info(f"Heart Platform [{avatar}] skipped to preserve Wind priority.")
                return False

        # 使用问心台
        reason = "late daily fallback" if late_fallback and not (8 <= curr_step <= 11) else "late cloud-stairs climb"
        log.info(f"Progress {curr_step}/12 for [{avatar}], Wind unavailable, sending .问心台 for {reason}.")
        hp_resp = await self.send_lingxiao_command(avatar, ".问心台", force_identity_check=True)
        if hp_resp:
            if self.lingxiao_unavailable(hp_resp):
                self.lingxiao_mismatch(avatar, hp_resp, source="Heart Platform")
                return False
            if any(k in hp_resp for k in ["问心台", "已经", "明天", "成功", "感受到", "感悟", "今日"]):
                self.set_lingxiao_state(avatar, "heart_platform_date", today)
                self.set_lingxiao_state(avatar, "last_heart_time", now_str())
                self.set_lingxiao_state(avatar, "next_heart_time", add_seconds_str(f"{today} 00:05:00", 24 * 3600))
                self.save_state()
                log.info(f"Heart Platform [{avatar}] used/confirmed for late cloud-stairs climb.")
                return True
            else:
                log.warning(f"Heart Platform [{avatar}] response unusual: {hp_resp[:100]}")
                notify_unrecognized_response(self, ".问心台", hp_resp, log, "问心台")
                return False
        return False

    async def lingxiao_tick(self, avatar):
        avatar = self.resolve_avatar_identity(avatar)
        if identity_sect(self, avatar) != "凌霄宫" or task_paused(self, avatar):
            return 60
        # ---- 1. 天阶状态检查 ----
        # 天阶状态只作为缓存缺失时的补账；正常登阶按固定 3 小时 CD 执行。
        next_stairs = self.lingxiao_restore_stairs_time(avatar)
        status_missing = not self.lingxiao_state(avatar).get("cloud_stairs_progress")
        if status_missing:
            log.info("Checking .天阶状态 (missing cached progress)...")
            status_resp = await self.send_lingxiao_command(avatar, ".天阶状态", force_identity_check=True)
        else:
            log.info(f"Cloud stairs cache valid. Skipping .天阶状态. Next Stairs: {next_stairs}")
            status_resp = None
        if status_resp:
            if self.lingxiao_unavailable(status_resp):
                self.lingxiao_mismatch(avatar, status_resp, source="Cloud stairs status")
                return 120
            self.lingxiao_record_progress(avatar, status_resp, source="Cloud stairs status")
            self.record_lingxiao_weeks(avatar, status_resp, source="Cloud stairs status")

            # 解析登阶冷却时间
            cd = self.parse_wait_time(status_resp, line_identifier="登阶冷却")
            if cd > 0:
                self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now_str(), cd))
                stairs_next = self.lingxiao_state(avatar).get("next_stairs_time", "")
                log.info(f"Cloud stairs CD: {cd}s, next run at {stairs_next}")
            elif "可立即登阶" in status_resp:
                self.set_lingxiao_state(avatar, "next_stairs_time", "")
                log.info("Cloud stairs ready immediately")

            # 解析引九天罡风冷却时间（从天阶状态中顺带解析，减少单独查询）
            wind_cd = self.parse_wait_time(status_resp, line_identifier="引九天罡风")
            if wind_cd > 0:
                self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", add_seconds_str(now_str(), wind_cd))
                wind_next = self.lingxiao_state(avatar).get("nine_heaven_wind_cd_time", "")
                log.info(f"Nine Heaven Wind CD: {wind_cd}s, next run at {wind_next}")
            elif "可立即施展" in status_resp or "引九天罡风" not in status_resp:
                # 如果没有冷却时间或未解锁引九天罡风，设置为 0 表示可用
                self.set_lingxiao_state(avatar, "nine_heaven_wind_cd_time", 0)

            self.save_state()

        # ---- 2. 登天阶执行 ----
        next_time_str = self.lingxiao_state(avatar).get("next_stairs_time", "")
        curr_step = self.lingxiao_step(avatar)
        today = datetime.now().strftime('%Y-%m-%d')

        await self.lingxiao_use_wind(avatar, source="Cloud Stairs")

        if self.sect_operation_allowed(avatar, ".登天阶") and (not next_time_str or not is_future(next_time_str)):
            # 登阶前先考虑是否用问心台
            await self.lingxiao_use_heart(avatar, curr_step, today)

            stairs_resp = await self.send_lingxiao_command(avatar, ".登天阶", timeout=120, force_identity_check=True)
            if stairs_resp:
                self.lingxiao_record_stairs(avatar, stairs_resp)
                self.save_state()
            else:
                self.set_lingxiao_state(avatar, "next_stairs_time", add_seconds_str(now_str(), 120))
                stairs_next = self.lingxiao_state(avatar).get("next_stairs_time", "")
                log.warning(f"Cloud stairs response missing; delaying retry until {stairs_next}.")
                self.save_state()
        elif self.lingxiao_state(avatar).get("heart_platform_date") != today and self.lingxiao_heart_fallback_due(today):
            # 登天阶 CD 中，但问心台保底时间到了
            await self.lingxiao_use_heart(avatar, curr_step, today, allow_daily_fallback=True)

        # ---- 3. 计算等待时间 ----
        next_stairs_str = self.lingxiao_state(avatar).get("next_stairs_time", "")
        wait_time = random.randint(10, 20)  # 如果 CD 到了，默认只睡一小会儿

        if next_stairs_str and is_future(next_stairs_str):
            wait_time = seconds_until(next_stairs_str) + random.randint(5, 15)
            log.info(f"Stairs CD active. Sleeping {wait_time}s until {next_stairs_str}")
        else:
            log.info(f"Stairs ready or no CD. Short sleep {wait_time}s before next attempt.")
            log.info(f"Cloud stairs next: {next_stairs_str}")

        # ---- 4. 问心台每日保底调度 ----
        # 如果今天还没用问心台，检查是否需要提前醒来执行保底
        if self.lingxiao_state(avatar).get("heart_platform_date") != today:
            fallback_time = self.lingxiao_heart_fallback_time(today)
            if is_future(fallback_time):
                heart_wait = seconds_until(fallback_time) + random.randint(5, 15)
                if heart_wait < wait_time:
                    wait_time = heart_wait
                    self.set_lingxiao_state(avatar, "next_heart_time", fallback_time)
                    self.save_state()
                    log.info(f"Heart Platform daily fallback pending. Sleeping {wait_time}s until {fallback_time}")

        wind_cd_time = self.lingxiao_state(avatar).get("nine_heaven_wind_cd_time", "")
        if (
            isinstance(wind_cd_time, str)
            and is_future(wind_cd_time)
            and not self.lingxiao_pending_wind(avatar)
        ):
            wind_wait = seconds_until(wind_cd_time) + random.randint(5, 15)
            if wind_wait < wait_time:
                wait_time = wind_wait
                log.info(f"Nine Heaven Wind pending. Sleeping {wait_time}s until {wind_cd_time}")

        variance = random.randint(10, 30)
        log.info(f"Cloud Stairs Loop Complete. Sleep {wait_time + variance}s.")
        return max(10, min(300, wait_time + variance))
