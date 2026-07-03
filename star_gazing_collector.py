"""
【观星事件采集与改换星移窗口预测】

三套脚本都会监听“星盘显化 / 天机异动”等消息，并把样本写入
star_gazing_events.jsonl。这个模块负责：
  1. 记录观星相关事件，供后续排查和统计。
  2. 根据最近样本预测 `.改换星移` 最合适的发送时间。
  3. 给不同账号提供 early / middle / dynamic 等错峰窗口。

注意：这里不直接发送 Telegram 消息，只提供记录和时间计算；真正发送在
intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 中。
"""
import hashlib
import json
import os
import random
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None


CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
EVENT_FILE = os.path.join(CONFIG_DIR, "star_gazing_events.jsonl")
LOCK_FILE = os.path.join(CONFIG_DIR, "star_gazing_events.lock")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

STAR_SHIFT_DEFAULT_SEND_RANGE = (21, 24)
STAR_SHIFT_PROFILE_WINDOWS = {
    "early": (-3, 2),   # 主号早窗：显化点前后轻微浮动，争取先手
    "middle": (3, 6),   # 副号中窗：避开主号，仍在结算前
}
STAR_SHIFT_MIN_SEND_DELAY_SECONDS = 6
STAR_SHIFT_MAX_SEND_DELAY_SECONDS = 28
STAR_SHIFT_RESULT_DELAY_SECONDS = 8
STAR_SHIFT_RESULT_SAFETY_GAP_RANGE = (2, 4)
STAR_SHIFT_MIN_RECENT_TYPE_SAMPLES = 5
STAR_SHIFT_MIN_RECENT_HOUR_SAMPLES = 5
STAR_SHIFT_MIN_RECENT_GLOBAL_SAMPLES = 10
STAR_SHIFT_HISTORY_LOOKBACK_DAYS = 7
STAR_SHIFT_RECENT_GLOBAL_DAYS = 3
STAR_SHIFT_NEWS_OFFSET_MAX_SECONDS = 120

STAR_EVENT_MARKERS = (
    # 这些关键词用于从群聊消息中识别“可能影响观星排期”的事件。
    "【星盘显化】",
    "【天机异动】",
    "【天机阁快报",
    "【Good -",
    "稳控全场",
    "控场",
    "静场令",
)

DEFAULT_GAME_BOT_USERNAMES = {
    "fanrenxiuxian_bot",
    "hantianzunhl",
    "hantianz_bot",
    "hantianzz_bot",
    "hantianzzz_bot",
    "hantianzzzz_bot",
    "hantianzzzzz_bot",
    "hantianzzzzzz_bot",
    "hantianzzzzzzz_bot",
    "hantianzzzzzzzz_bot",
}


def _local_tz():
    if ZoneInfo:
        return ZoneInfo("Asia/Shanghai")
    return timezone(timedelta(hours=8))


@contextmanager
def _exclusive_file_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _message_local_time(msg):
    dt = getattr(msg, "date", None)
    if not dt:
        return datetime.now(_local_tz()).replace(tzinfo=None)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz()).replace(tzinfo=None)


def _boundary_time(dt):
    return dt.replace(hour=(dt.hour // 3) * 3, minute=0, second=0, microsecond=0)


def _next_boundary_time(dt):
    boundary = _boundary_time(dt)
    if dt >= boundary:
        boundary += timedelta(hours=3)
    return boundary


def _fmt_dt(dt):
    return dt.strftime(TIME_FORMAT) if dt else ""


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), TIME_FORMAT)
    except Exception:
        return None


def _median(values):
    values = sorted(float(v) for v in values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _fate_title(value):
    value = _strip_md(value or "")
    match = re.search(r"(?:Good|Bad|Neutral)\s*-\s*(.+)", value)
    if match:
        return match.group(1).strip()
    return value.strip("【】 ")


def _load_star_gazing_records(path=EVENT_FILE):
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return records


def _unique_news_offsets(records, now=None):
    now = now or datetime.now(_local_tz()).replace(tzinfo=None)
    seen = set()
    items = []
    for record in records:
        if record.get("event_kind") != "news":
            continue
        offset = record.get("final_news_offset_seconds")
        if not isinstance(offset, (int, float)):
            continue
        if offset < 0 or offset > STAR_SHIFT_NEWS_OFFSET_MAX_SECONDS:
            continue
        message_time = _parse_dt(record.get("message_time"))
        target_time = _parse_dt(record.get("target_manifest_time"))
        if not message_time or not target_time:
            continue
        key = (
            record.get("target_manifest_time", ""),
            record.get("message_id"),
            record.get("text_hash", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "offset": float(offset),
                "title": _fate_title(record.get("news_title")),
                "hour": target_time.hour,
                "message_time": message_time,
            }
        )
    return items


def predict_star_shift_delay_range(
    target_dt,
    fate_type="",
    now=None,
    history_file=EVENT_FILE,
    shift_profile="dynamic",
):
    """Predict a .改换星移 send window from a fixed profile or collected history."""
    now = now or datetime.now(_local_tz()).replace(tzinfo=None)
    target_dt = target_dt.replace(tzinfo=None)
    profile = str(shift_profile or "dynamic").strip().lower()
    if profile in STAR_SHIFT_PROFILE_WINDOWS:
        min_delay, max_delay = STAR_SHIFT_PROFILE_WINDOWS[profile]
        return min_delay, max_delay, f"{profile} fixed window"

    title = _fate_title(fate_type)
    news_items = _unique_news_offsets(_load_star_gazing_records(history_file), now=now)

    def choose(candidates, min_count, reason):
        if len(candidates) < min_count:
            return None, ""
        value = _median(item["offset"] for item in candidates)
        return value, f"{reason} n={len(candidates)}"

    predicted_news_offset = None
    reason = ""
    recent_cutoff = now - timedelta(days=STAR_SHIFT_HISTORY_LOOKBACK_DAYS)
    recent_global_cutoff = now - timedelta(days=STAR_SHIFT_RECENT_GLOBAL_DAYS)

    if title:
        predicted_news_offset, reason = choose(
            [
                item for item in news_items
                if item["title"] == title and item["message_time"] >= recent_cutoff
            ],
            STAR_SHIFT_MIN_RECENT_TYPE_SAMPLES,
            f"recent type {title}",
        )

    if predicted_news_offset is None:
        predicted_news_offset, reason = choose(
            [
                item for item in news_items
                if item["hour"] == target_dt.hour and item["message_time"] >= recent_cutoff
            ],
            STAR_SHIFT_MIN_RECENT_HOUR_SAMPLES,
            f"recent hour {target_dt.hour:02d}",
        )

    if predicted_news_offset is None:
        predicted_news_offset, reason = choose(
            [item for item in news_items if item["message_time"] >= recent_global_cutoff],
            STAR_SHIFT_MIN_RECENT_GLOBAL_SAMPLES,
            f"recent {STAR_SHIFT_RECENT_GLOBAL_DAYS}d global",
        )

    if predicted_news_offset is None:
        predicted_news_offset, reason = choose(
            [item for item in news_items if item["message_time"] >= recent_cutoff],
            STAR_SHIFT_MIN_RECENT_GLOBAL_SAMPLES,
            f"recent {STAR_SHIFT_HISTORY_LOOKBACK_DAYS}d global",
        )

    if predicted_news_offset is None:
        predicted_news_offset, reason = choose(news_items, 1, "all history")

    if predicted_news_offset is None:
        return (*STAR_SHIFT_DEFAULT_SEND_RANGE, "default no history")

    base_delay = int(
        round(
            predicted_news_offset
            - STAR_SHIFT_RESULT_DELAY_SECONDS
            - _median(STAR_SHIFT_RESULT_SAFETY_GAP_RANGE)
        )
    )
    min_delay = max(STAR_SHIFT_MIN_SEND_DELAY_SECONDS, base_delay - 1)
    max_delay = min(STAR_SHIFT_MAX_SEND_DELAY_SECONDS, base_delay + 1)
    if min_delay > max_delay:
        min_delay = max_delay
    return min_delay, max_delay, f"{reason}, news~+{predicted_news_offset:.1f}s"


def predicted_star_shift_dt(
    target_dt,
    now=None,
    fate_type="",
    history_file=EVENT_FILE,
    logger=None,
    shift_profile="dynamic",
):
    """Return a .改换星移 send time around the manifest boundary."""
    now = now or datetime.now(_local_tz()).replace(tzinfo=None)
    min_delay, max_delay, reason = predict_star_shift_delay_range(
        target_dt,
        fate_type=fate_type,
        now=now,
        history_file=history_file,
        shift_profile=shift_profile,
    )
    elapsed = (now - target_dt).total_seconds()
    if elapsed > min_delay:
        min_delay = min(max_delay, max(min_delay, int(elapsed) + 1))
    delay = random.randint(int(min_delay), int(max_delay))
    if logger:
        profile = str(shift_profile or "dynamic").strip().lower()
        window = f"{min_delay:+d}~{max_delay:+d}s"
        logger.info(
            f"Star gazing: {profile} shift window {window} "
            f"for {target_dt.strftime(TIME_FORMAT)} ({fate_type or 'unknown fate'}; {reason}); "
            f"selected {delay:+d}s."
        )
    return target_dt + timedelta(seconds=delay)


def _strip_md(value):
    return (value or "").replace("*", "").strip()


def _is_game_bot_sender(sender):
    username = (getattr(sender, "username", "") or "").lower().lstrip("@")
    return bool(username and username in DEFAULT_GAME_BOT_USERNAMES)


def _first_match(patterns, text, flags=0):
    for pattern in patterns:
        match = re.search(pattern, text or "", flags)
        if match:
            return match
    return None


def _extract_fate_type(text):
    match = re.search(r"【((?:Good|Bad|Neutral)\s*-\s*[^】]+)】", text or "")
    return match.group(1).strip() if match else ""


def _extract_news_title(text):
    match = re.search(r"【天机阁快报\s*-\s*([^】]+)】", text or "")
    return match.group(1).strip() if match else ""


def _extract_manifest_fields(text):
    observer = ""
    current_target = ""

    match = re.search(r"(@\S+)\s+闭目凝神", text or "")
    if match:
        observer = _strip_md(match.group(1))

    for raw_line in (text or "").splitlines():
        line = _strip_md(raw_line)
        if "当前天命所归" in line and ":" in line:
            current_target = line.split(":", 1)[1].strip()
            break

    return observer, current_target


def _extract_shift_fields(text):
    actor = ""
    original_target = ""
    shift_target = ""

    match = re.search(r"弟子\s+(@\S+)\s+强行施展", text or "")
    if match:
        actor = _strip_md(match.group(1))

    match = _first_match(
        (
            r"原本将降临于\s+(.*?)\s+身上的\*+【(?:Good|Bad|Neutral)\s*-\s*[^】]+】\*+.*?将由\s+\**(.*?)\**\s+承受",
            r"原本将降临于\s+(.*?)\s+身上的\s*【(?:Good|Bad|Neutral)\s*-\s*[^】]+】.*?将由\s+\**(.*?)\**\s+承受",
        ),
        text,
        flags=re.S,
    )
    if match:
        original_target = _strip_md(match.group(1))
        shift_target = _strip_md(match.group(2))

    return actor, original_target, shift_target


def _classify_event(text):
    if any(marker in (text or "") for marker in ("稳控全场", "控场", "静场令")):
        return "control"
    if "【天机阁快报" in (text or ""):
        return "news"
    if "【天机异动】" in (text or ""):
        return "shift"
    if "【星盘显化】" in (text or ""):
        return "manifest"
    if "【Good -" in (text or ""):
        return "good_message"
    return ""


def _target_manifest_time(event_kind, local_time):
    boundary = _boundary_time(local_time)
    offset = (local_time - boundary).total_seconds()
    if event_kind == "news":
        return boundary
    if event_kind == "manifest":
        if 0 < offset <= 120:
            return boundary
        return _next_boundary_time(local_time)
    if event_kind == "shift":
        return boundary if 0 <= offset <= 120 else _next_boundary_time(local_time)
    if event_kind == "control":
        return boundary if 0 <= offset <= 300 else _next_boundary_time(local_time)
    return _next_boundary_time(local_time)


def _dedupe_key(record):
    base = "|".join(
        str(record.get(part, ""))
        for part in ("chat_id", "message_id", "event_kind", "is_edited", "text_hash")
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _load_existing_keys(path):
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            key = item.get("dedupe_key")
            if key:
                keys.add(key)
    return keys


def build_star_gazing_event_record(account, msg, text, sender=None, is_edited=False):
    text = text or ""
    if sender is not None and not _is_game_bot_sender(sender):
        return None
    if not any(marker in text for marker in STAR_EVENT_MARKERS):
        return None

    event_kind = _classify_event(text)
    if not event_kind:
        return None

    local_time = _message_local_time(msg)
    boundary = _boundary_time(local_time)
    target_manifest = _target_manifest_time(event_kind, local_time)
    offset_seconds = round((local_time - boundary).total_seconds(), 3)

    observer, current_target = _extract_manifest_fields(text)
    shift_actor, original_target, shift_target = _extract_shift_fields(text)
    fate_type = _extract_fate_type(text)
    news_title = _extract_news_title(text)
    text_hash = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()

    record = {
        "schema_version": 1,
        "recorded_at": datetime.now(_local_tz()).strftime(TIME_FORMAT),
        "account": account,
        "event_kind": event_kind,
        "is_edited": bool(is_edited),
        "message_time": _fmt_dt(local_time),
        "message_boundary_time": _fmt_dt(boundary),
        "offset_seconds_from_boundary": offset_seconds,
        "target_manifest_time": _fmt_dt(target_manifest),
        "final_news_offset_seconds": offset_seconds if event_kind == "news" else None,
        "chat_id": getattr(msg, "chat_id", None),
        "message_id": getattr(msg, "id", None),
        "sender_id": getattr(msg, "sender_id", None),
        "sender_username": getattr(sender, "username", "") or "",
        "sender_first_name": getattr(sender, "first_name", "") or "",
        "fate_type": fate_type,
        "news_title": news_title,
        "control_keyword": "稳控全场" if "稳控全场" in text else "控场" if "控场" in text else "",
        "control_actor": "",
        "control_context": "",
        "control_is_pending_until_instance_start": bool(
            "组队阶段不会生效" in text and "正式进入副本后才会激活" in text
        ),
        "observer": observer,
        "current_target": current_target,
        "shift_actor": shift_actor,
        "original_target": original_target,
        "shift_target": shift_target,
        "text_hash": text_hash,
        "text": text,
    }
    record["dedupe_key"] = _dedupe_key(record)
    return record


def record_star_gazing_event(account, msg, text, sender=None, is_edited=False, logger=None):
    record = build_star_gazing_event_record(account, msg, text, sender=sender, is_edited=is_edited)
    if not record:
        return False

    try:
        with _exclusive_file_lock(LOCK_FILE):
            keys = _load_existing_keys(EVENT_FILE)
            if record["dedupe_key"] in keys:
                return False
            with open(EVENT_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception as exc:
        if logger:
            logger.warning(f"Star gazing collector write failed: {exc}")
        return False
