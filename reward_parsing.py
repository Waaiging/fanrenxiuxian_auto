#!/usr/bin/env python3
"""Shared reward parsing helpers for runtime records and Dashboard display."""

from __future__ import annotations

import re
from typing import Any


RESOURCE_NAMES = (
    "修为",
    "灵石",
    "宗门贡献",
    "贡献",
    "神识",
    "气血",
    "煞气",
    "道韵",
    "感悟",
    "经验",
    "星辰精华",
    "精华",
    "天机值",
    "天机",
    "塔印",
    "边境军功",
)
RESOURCE_PATTERN = "|".join(map(re.escape, RESOURCE_NAMES))

BRACKET_REWARD_STOP_NAMES = {
    "深度闭关总结",
    "元婴闭关结算",
    "元婴归窍总结",
    "元神归窍总结",
    "元婴成长",
    "探寻成功",
    "不敌败退",
    "遭遇风暴",
    "激战得胜",
    "大凶·虚空噬体",
    "元婴遁逃·虚弱",
    "凌霄云阶",
    "天门洞开",
    "周天巡天",
    "天门余韵",
    "罡风淬体",
    "天人感应",
    "推命命中",
    "改命待发",
    "天星偏转",
    "改命回天",
    "战斗加成",
    "凌霄神通",
    "问道得宝",
    "慕兰烽烟",
}

REWARD_PRIORITY = {
    "修为": 0,
    "天机": 1,
    "天机值": 1,
    "宗门贡献": 2,
    "贡献": 2,
    "边境军功": 3,
    "塔印": 4,
    "灵石": 5,
    "神识": 6,
    "气血": 7,
    "煞气": 8,
    "道韵": 9,
    "感悟": 10,
    "经验": 11,
    "星辰精华": 12,
    "精华": 13,
}


def clean_reward_text(text: Any) -> str:
    return re.sub(
        r"[ \t]+",
        " ",
        str(text or "").replace("**", "").replace("`", ""),
    ).strip()


def reward_command_root(command: Any) -> str:
    text = str(command or "").strip()
    return text.split()[0] if text else ""


def canonical_reward_name(name: Any) -> str:
    text = str(name or "").strip()
    return {
        "贡献": "宗门贡献",
        "天机值": "天机",
        "精华": "星辰精华",
    }.get(text, text)


def merge_reward_items(target: dict[str, int], rewards: Any) -> dict[str, int]:
    if not isinstance(rewards, dict):
        return target
    for raw_name, raw_value in rewards.items():
        name = canonical_reward_name(raw_name)
        if not name:
            continue
        try:
            value = int(raw_value or 0)
        except (TypeError, ValueError):
            continue
        if value:
            target[name] = int(target.get(name, 0) or 0) + value
    return target


def field_training_settlement_text(text: Any) -> str:
    clean = clean_reward_text(text)
    if not clean:
        return ""

    title = ""
    title_match = re.search(r"【野外历练[^】]{0,30}】", clean)
    if title_match:
        title = title_match.group(0)

    candidate_starts: list[int] = []
    for pattern in (
        r"@[A-Za-z0-9_]{2,}\s*(?:遭遇|在|采得|发现|误入|寻得|负伤|一时|本已|选择)",
        r"@\S{2,40}\s*遭遇",
        r"战力对比\s*[:：]",
        r"一番斗法后",
        r"本已要负伤折返",
    ):
        match = re.search(pattern, clean)
        if match and (not title_match or match.start() > title_match.end()):
            candidate_starts.append(match.start())

    if not candidate_starts:
        return clean
    tail = clean[min(candidate_starts):].strip()
    if not tail:
        return clean
    if title and title not in tail[:80]:
        return f"{title} {tail}".strip()
    return tail


def rift_settlement_text(text: Any) -> str:
    clean = clean_reward_text(text)
    if not clean:
        return ""

    candidate_starts: list[int] = []
    for pattern in (
        r"你的元婴满载而归",
        r"一番斗法后",
        r"获得修为\s*[+＋-]?\d",
        r"【遭遇风暴】",
        r"【不敌败退】",
        r"【大凶·虚空噬体】",
        r"【元婴遁逃·虚弱】",
        r"身受重创",
        r"遭受重创",
        r"虚弱期",
        r"肉身破碎",
        r"肉身化为",
        r"修为倒退",
    ):
        match = re.search(pattern, clean)
        if match:
            candidate_starts.append(match.start())
    return clean[min(candidate_starts):].strip() if candidate_starts else clean


def tower_settlement_text(text: Any) -> str:
    """Keep only actual pagoda gains/losses, excluding floor/build metadata."""
    clean = clean_reward_text(text)
    if not clean:
        return ""
    start_match = re.search(r"(?:^|\n)\s*总收获\s*[:：]\s*", clean)
    if not start_match:
        return clean

    start = start_match.end()
    end = len(clean)
    for pattern in (
        r"(?:^|\n)\s*本次塔相轨迹\s*[:：]",
        r"(?:^|\n)\s*本次构筑\s*[:：]",
        r"(?:^|\n)\s*触发奇遇\s*[:：]",
        r"(?:^|\n)\s*遭遇词缀\s*[:：]",
        r"(?:^|\n)\s*同境界进度\s*[:：]",
    ):
        match = re.search(pattern, clean[start:])
        if match:
            end = min(end, start + match.start())

    parts = [clean[start:end].strip()]
    for match in re.finditer(
        r"未消耗塔钥[^\n。]{0,120}?折算为\s*修为\s*[+-]?\d[\d,]*\s*[、,，]\s*贡献\s*[+-]?\d[\d,]*",
        clean,
    ):
        parts.append(match.group(0).strip())
    return "\n".join(part for part in parts if part)


def reward_text_for_command(command: Any, text: Any) -> str:
    root = reward_command_root(command)
    if root == ".野外历练":
        return field_training_settlement_text(text)
    if root == ".探寻裂缝":
        return rift_settlement_text(text)
    if root == ".闯塔":
        return tower_settlement_text(text)
    return clean_reward_text(text)


def parse_reward_items(text: Any) -> dict[str, int]:
    """Best-effort parser for quantified and clearly contextual rewards."""
    clean = clean_reward_text(text)
    if not clean:
        return {}
    rewards: dict[str, int] = {}

    def add(raw_name: Any, raw_amount: Any) -> None:
        name = str(raw_name or "").strip(" ：:，,。.;；-+")
        if not name:
            return
        if name in {
            "x", "X", "本次", "额外", "收益", "奖励", "获得", "收获", "共计",
            "未消耗塔钥",
        }:
            return
        if name in BRACKET_REWARD_STOP_NAMES:
            return
        if any(ch in name for ch in "【】[]"):
            return
        name = canonical_reward_name(name)
        try:
            value = int(str(raw_amount).replace(",", ""))
        except (TypeError, ValueError):
            return
        if value:
            rewards[name] = int(rewards.get(name, 0) or 0) + value

    for name, amount in re.findall(r"【([^】]{1,30})】\s*[xX*＊]\s*([+-]?\d[\d,]*)", clean):
        add(name, amount)

    for match in re.finditer(r"【([^】]{1,30})】(?!\s*[xX*＊]\s*[+-]?\d)", clean):
        name = match.group(1).strip()
        if not name or name in BRACKET_REWARD_STOP_NAMES or name.startswith("野外历练"):
            continue
        before = clean[max(0, match.start() - 16):match.start()]
        after = clean[match.end():min(len(clean), match.end() + 24)]
        if "灵兽" in before and re.match(r"\s*(?:成功|击败|出战|休息|已)", after):
            continue
        if re.match(r"\s*(?:因与|，?斗法|照命|成功击败|已助阵|正在|尚需)", after):
            continue
        context_before = clean[max(0, match.start() - 44):match.start()]
        if any(marker in context_before for marker in (
            "为你带来了",
            "带来了",
            "带回了",
            "获得了",
            "获得",
            "得到",
            "收获",
            "发现",
            "意外之喜",
            "奖励",
            "战利品",
            "至宝",
            "额外收获",
        )):
            add(name, 1)

    for name, amount in re.findall(
        r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·（）()]{0,20})\s*[xX*＊]\s*([+-]?\d[\d,]*)",
        clean,
    ):
        add(name, amount)

    for name, amount in re.findall(
        rf"({RESOURCE_PATTERN})\s*(?:额外)?\s*(?:增加了?|提升了?|获得了?|得到了?|为|:|：)?\s*\+?\s*([+-]?\d[\d,]*)",
        clean,
    ):
        add(name, amount)

    for name, amount in re.findall(
        rf"({RESOURCE_PATTERN})\s*(?:减少了?|降低了?|扣除|扣了?|损失了?|折损了?|倒退了?|消耗了?)\s*\+?\s*([+-]?\d[\d,]*)",
        clean,
    ):
        try:
            value = -abs(int(str(amount).replace(",", "")))
        except (TypeError, ValueError):
            continue
        add(name, value)

    negative_before_amount = ("减少", "降低", "扣除", "扣了", "损失", "折损", "倒退", "消耗")
    for match in re.finditer(
        rf"([+-]?\d[\d,]*)[ \t]*(点|枚|份|缕|颗|个)?[ \t]*({RESOURCE_PATTERN})",
        clean,
    ):
        prefix = clean[max(0, match.start() - 12):match.start()]
        if any(word in prefix for word in negative_before_amount):
            continue
        amount, _unit, name = match.groups()
        add(name, amount)

    for amount in re.findall(r"修为最终(?:增加|变化)了?\s*\+?\s*([+-]?\d[\d,]*)\s*点", clean):
        add("修为", amount)
    return rewards


def context_reward_items(command: Any, text: Any) -> dict[str, int]:
    clean = clean_reward_text(text)
    if not clean:
        return {}
    rewards: dict[str, int] = {}

    def add(name: str, amount: Any) -> None:
        try:
            value = int(str(amount).replace(",", ""))
        except (TypeError, ValueError):
            return
        if value:
            key = canonical_reward_name(name)
            rewards[key] = int(rewards.get(key, 0) or 0) + value

    root = reward_command_root(command)
    if root == ".问道":
        if "大道感悟" in clean or "获得感悟" in clean:
            add("感悟", 1)
        elif "道韵" in clean and not re.search(r"道韵\s*\+?\s*\d", clean):
            add("道韵", 1)

    context_lines = [
        line
        for line in re.split(r"[\n\r]+", clean)
        if any(marker in line for marker in ("推命命中", "司命演算", "天机值"))
    ]
    context = "\n".join(context_lines)
    for amount in re.findall(r"天机值\s*\+?\s*([+-]?\d[\d,]*)", context):
        add("天机", amount)
    for _name, amount in re.findall(r"(宗门贡献|贡献)\s*\+?\s*([+-]?\d[\d,]*)", context):
        add("宗门贡献", amount)
    return rewards


def daily_reward_items_for_command(command: Any, text: Any) -> dict[str, int]:
    root = reward_command_root(command)
    rewards = parse_reward_items(reward_text_for_command(command, text))
    # Destiny-side gains are real for rifts/other commands, but the field-
    # training settlement intentionally excludes its preparatory divination.
    if root != ".野外历练":
        merge_reward_items(rewards, context_reward_items(command, text))
    return rewards


def is_mulan_settlement_text(text: Any) -> bool:
    clean = clean_reward_text(text)
    if not clean or "慕兰烽烟" not in clean:
        return False
    if "正赶往天南边境" in clean and not any(
        marker in clean for marker in ("边境军功", "获得修为", "获得灵石", "获得材料")
    ):
        return False
    return any(marker in clean for marker in (
        "边境军功",
        "获得修为",
        "获得灵石",
        "获得材料",
        "小胜",
        "险还",
        "败退",
    ))


def trust_empty_reward_reparse(command: Any, text: Any) -> bool:
    """Whether detailed raw text is authoritative when reparsing finds no rewards."""
    root = reward_command_root(command)
    clean = clean_reward_text(text)
    if not clean:
        return False
    if root == ".支援慕兰":
        return is_mulan_settlement_text(clean)
    if root == ".闯塔":
        return any(marker in clean for marker in (
            "试炼古塔 - 战报",
            "总收获",
            "本次共闯过",
            "闯塔历程",
        ))
    if root in {".探渊", ".灵兽探渊"}:
        return any(marker in clean for marker in (
            "成功击败了对手",
            "带回了战利品",
            "战利品归来",
            "探渊胜利",
            "重伤退回",
            "不敌",
            "败退",
            "任务失败",
        ))
    return False


def normalize_reward_items(rewards: Any) -> dict[str, int]:
    return merge_reward_items({}, rewards)


def compact_reward_summary(rewards: Any) -> str:
    normalized = normalize_reward_items(rewards)
    if not normalized:
        return ""

    plus_names = {
        "修为",
        "天机",
        "宗门贡献",
        "边境军功",
        "塔印",
        "神识",
        "气血",
        "煞气",
        "道韵",
        "感悟",
        "经验",
        "星辰精华",
    }

    def display_name(name: str) -> str:
        return {
            "宗门贡献": "贡献",
            "天机值": "天机",
        }.get(name, name)

    def sort_key(item: tuple[str, int]) -> tuple[int, str]:
        name = item[0]
        return REWARD_PRIORITY.get(name, 100), display_name(name)

    parts: list[str] = []
    for name, amount in sorted(normalized.items(), key=sort_key):
        if not amount:
            continue
        shown = display_name(name)
        number = f"{int(amount):,}"
        if amount < 0:
            parts.append(f"{shown}{number}")
        elif name in plus_names:
            parts.append(f"{shown}+{number}")
        else:
            parts.append(f"{shown}x{number}")
    return "｜".join(parts)
