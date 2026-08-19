#!/usr/bin/env python3
"""Shared daily Mini App automation for pagoda and dwelling treasure hunts."""

from __future__ import annotations

import asyncio
import math
import random
import re
from datetime import datetime, timedelta
from typing import Any

import networkx as nx

from automation_settings import (
    load_automation_settings,
    miniapp_fate_cards_identities_for_account,
    miniapp_fate_cards_settings,
    miniapp_tianji_trial_identities_for_account,
    miniapp_tianji_trial_settings,
)
from miniapp_beast import (
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    miniapp_circuit_preflight,
    miniapp_circuit_wait_seconds,
)
from miniapp_dwelling import apply_dwelling_snapshot, command_result_ok, identity_state


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_HUNT_HOUR = 7
DEFAULT_PAGODA_HOUR = 23
DEFAULT_TIANJI_TRIAL_HOUR = 8
DEFAULT_FATE_CARDS_HOUR = 9
DEFAULT_DAILY_RETRY_SECONDS = 15 * 60
DEFAULT_TARGET_REWARD = "阴凝之晶"
ACCOUNT_MINUTE_OFFSETS = {
    "main": 0,
    "sub": 10,
    "xiaohao": 20,
    "waaiging": 30,
}
HUNT_DIRECTION_RE = re.compile(
    r"灵气流向\s*(东北|北东|东南|南东|西北|北西|西南|南西|北|南|东|西|此地)"
)
HUNT_DIRECTION_ALIASES = {
    "东北": "北东",
    "东南": "南东",
    "西北": "北西",
    "西南": "南西",
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def hunt_counter(payload: Any) -> dict[str, int]:
    dwelling = payload.get("dwelling") if isinstance(payload, dict) else {}
    dwelling = dwelling if isinstance(dwelling, dict) else {}
    hunt = dwelling.get("hunt") if isinstance(dwelling.get("hunt"), dict) else {}
    limit = max(0, int(hunt.get("limit") or 0))
    used = max(0, int(hunt.get("used") or 0))
    remaining = hunt.get("remaining")
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        remaining = max(0, limit - used)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, remaining),
        "action_points": max(0, int(hunt.get("actionPoints") or 0)),
    }


def hunt_run(payload: Any) -> dict[str, Any]:
    run = payload.get("huntRun") if isinstance(payload, dict) else None
    return run if isinstance(run, dict) else {}


def hunt_loot_contains(run: Any, target: str) -> bool:
    target = str(target or "").strip()
    if not target or not isinstance(run, dict):
        return False
    for item in run.get("loot") or []:
        if not isinstance(item, dict):
            continue
        if target in str(item.get("name") or ""):
            return True
    return False


def hunt_result_loot(payload: Any) -> dict[str, int]:
    result = payload.get("huntResult") if isinstance(payload, dict) else {}
    result = result if isinstance(result, dict) else {}
    loot = result.get("loot") if isinstance(result.get("loot"), list) else []
    merged: dict[str, int] = {}
    for item in loot:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "物品").strip() or "物品"
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        merged[name] = merged.get(name, 0) + quantity
    return merged


def normalize_hunt_loot(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_name, raw_quantity in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        try:
            quantity = int(raw_quantity or 0)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            normalized[name] = quantity
    return normalized


def merge_hunt_loot(total: Any, payload: Any) -> dict[str, int]:
    merged = normalize_hunt_loot(total)
    for name, quantity in hunt_result_loot(payload).items():
        merged[name] = merged.get(name, 0) + quantity
    return merged


def hunt_loot_text(total: Any) -> str:
    loot = normalize_hunt_loot(total)
    return "，".join(f"{name} x{quantity}" for name, quantity in loot.items()) or "无物品"


def pagoda_result_text(payload: Any) -> str:
    replay = payload.get("replay") if isinstance(payload, dict) else {}
    replay = replay if isinstance(replay, dict) else {}
    report = str(replay.get("report") or "").strip()
    if report:
        return report
    return (
        f"琉璃问心塔通过 {int(replay.get('clearedCount') or 0)} 层，"
        f"抵达第 {int(replay.get('endFloor') or 0)} 层，"
        f"止步第 {int(replay.get('failedFloor') or 0)} 层"
    )


def tianji_trial_progress(payload: Any) -> tuple[int, int]:
    progress = payload.get("dailyProgress") if isinstance(payload, dict) else {}
    progress = progress if isinstance(progress, dict) else {}
    result = payload.get("result") if isinstance(payload, dict) else {}
    result = result if isinstance(result, dict) else {}
    completed = int(progress.get("completed") or result.get("daily_progress") or 0)
    limit = int(progress.get("limit") or result.get("daily_limit") or 3)
    return max(0, completed), max(1, limit)


def tianji_trial_result_text(results: list[dict[str, Any]]) -> str:
    rewards = 0
    bonuses = 0
    grades = []
    balance = 0
    for payload in results:
        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        rewards += int(result.get("reward_trace") or 0)
        bonuses += int(result.get("bonus_trace") or 0)
        balance = int(result.get("balance") or balance)
        grade = str(result.get("grade") or "").strip()
        if grade:
            grades.append(grade)
    grade_text = "/".join(grades) or "已结算"
    extra = f"（额外 {bonuses}）" if bonuses > 0 else ""
    completed, limit = tianji_trial_progress(results[-1]) if results else (0, 3)
    return (
        f"{completed or limit} 关完成，评级 {grade_text}，"
        f"天机残痕 +{rewards}{extra}，余额 {balance}"
    )


def tianji_trial_log_text(results: list[dict[str, Any]]) -> str:
    """Build a readable per-stage Tianji trial summary for the runtime log."""
    completed, limit = tianji_trial_progress(results[-1]) if results else (0, 3)
    title = f"天机试炼汇总（今日完成 {completed}/{limit} 关"
    if len(results) != completed:
        title += f"，本次记录 {len(results)} 关"
    title += "）"

    lines = []
    rewards = 0
    bonuses = 0
    balance = 0
    for index, payload in enumerate(results, start=1):
        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        stage = int(result.get("daily_progress") or index)
        grade = str(result.get("grade") or "已结算").strip() or "已结算"
        reward = int(result.get("reward_trace") or 0)
        bonus = int(result.get("bonus_trace") or 0)
        rewards += reward
        bonuses += bonus
        balance = int(result.get("balance") or balance)
        extra = f"（额外 {bonus}）" if bonus > 0 else ""
        lines.append(
            f"{len(lines) + 1}. 第 {stage} 关：{grade}，天机残痕 +{reward}{extra}"
        )

    total_extra = f"（额外 {bonuses}）" if bonuses > 0 else ""
    lines.append(f"合计：天机残痕 +{rewards}{total_extra}，余额 {balance}")
    return "\n".join([title, *lines])


FATE_CARD_CHOICE_NAMES = {
    "accept": "顺势承命",
    "hide": "藏锋避劫",
}


def fate_cards_record(payload: Any) -> dict[str, Any]:
    record = payload.get("record") if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else {}


def fate_cards_quest(record: Any) -> dict[str, Any]:
    quest = record.get("quest") if isinstance(record, dict) else None
    return quest if isinstance(quest, dict) else {}


def fate_cards_ai_ready(record: Any) -> bool:
    reading = record.get("aiReading") if isinstance(record, dict) else None
    return bool(isinstance(reading, dict) and str(reading.get("overview") or "").strip())


def fate_cards_wait_seconds(quest: Any, now: datetime | None = None) -> int:
    if not isinstance(quest, dict) or quest.get("canSettle"):
        return 0
    try:
        target = max(0, int(quest.get("target") or 0))
        progress = max(0, int(quest.get("progress") or 0))
    except (TypeError, ValueError):
        return 0
    remaining = max(0, target - progress)
    started_text = str(quest.get("startedAt") or "").strip()
    if not started_text or target <= 0:
        return remaining
    try:
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        current = now or datetime.now(started.tzinfo)
        if started.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=started.tzinfo)
        elapsed = max(0, int((current - started).total_seconds()))
        remaining = min(remaining, max(0, target - elapsed))
    except (TypeError, ValueError):
        pass
    return remaining


def fate_cards_result_text(payload: Any, record: Any = None) -> str:
    source = payload if isinstance(payload, dict) else {}
    record = record if isinstance(record, dict) else fate_cards_record(source)
    question = record.get("question") if isinstance(record.get("question"), dict) else {}
    question_name = str(question.get("name") or "机缘").strip() or "机缘"
    cards = []
    for card in record.get("cards") or []:
        if not isinstance(card, dict):
            continue
        position = str(card.get("positionName") or "命牌").strip() or "命牌"
        title = str(card.get("title") or "无名命牌").strip() or "无名命牌"
        orientation = str(card.get("orientation") or "").strip()
        cards.append(f"{position}：{title}{f'（{orientation}）' if orientation else ''}")
    choice = str(record.get("choiceKey") or "").strip()
    quest = fate_cards_quest(record)
    reading = record.get("aiReading") if isinstance(record.get("aiReading"), dict) else {}
    overview = re.sub(r"\s+", " ", str(reading.get("overview") or "").strip())[:180]
    reward = source.get("reward") if isinstance(source.get("reward"), dict) else {}
    trace = max(0, int(reward.get("tianjiTrace") or 0))
    pass_count = max(0, int(reward.get("kunwuPass") or 0))
    balance = reward.get("balance")
    reward_parts = [f"天机残痕 +{trace}"]
    if pass_count:
        reward_parts.append(f"昆吾通行令 +{pass_count}")
    if balance is not None:
        reward_parts.append(f"余额 {int(balance or 0)}")
    card_text = "；".join(cards) or "命牌已取回"
    quest_title = str(quest.get("title") or "命脉任务").strip() or "命脉任务"
    settled = str(quest.get("status") or "") == "settled" or bool(source.get("alreadySettled"))
    settlement = (
        "今日已结算"
        if source.get("alreadySettled") or (settled and not reward)
        else "、".join(reward_parts)
    )
    if not settled and not reward:
        settlement = "等待验命"
    return (
        f"问天：{question_name}；命牌：{card_text}；"
        f"命解：{overview or '已完成'}；"
        f"命择：{FATE_CARD_CHOICE_NAMES.get(choice, choice or '未选择')}；"
        f"任务：{quest_title}；验命：{settlement}"
    )[:1200]


def _trial_duration_ms(challenge: dict[str, Any], event_count: int = 1) -> int:
    minimum = max(350, int(challenge.get("minDurationMs") or challenge.get("min_duration_ms") or 3200))
    return minimum + max(400, int(event_count) * 80)


def _trial_lights_neighbors(index: int, size: int) -> list[int]:
    row, column = divmod(index, size)
    result = [index]
    if row > 0:
        result.append(index - size)
    if row < size - 1:
        result.append(index + size)
    if column > 0:
        result.append(index - 1)
    if column < size - 1:
        result.append(index + 1)
    return result


def _solve_lights_out(challenge: dict[str, Any]) -> dict[str, Any]:
    size = max(4, min(5, int(challenge.get("gridSize") or challenge.get("grid_size") or 4)))
    cells = [1 if int(value or 0) else 0 for value in (challenge.get("cells") or [])]
    if len(cells) != size * size:
        raise MiniAppBeastError("trial_lights_invalid")
    target = 1 if int(challenge.get("targetState", challenge.get("target_state", 1)) or 0) else 0
    count = size * size
    rows = []
    right = []
    for cell in range(count):
        mask = 0
        for press in range(count):
            if cell in _trial_lights_neighbors(press, size):
                mask |= 1 << press
        rows.append(mask)
        right.append(cells[cell] ^ target)

    pivot_columns: list[int] = []
    pivot_row = 0
    for column in range(count):
        selected = next(
            (row for row in range(pivot_row, count) if (rows[row] >> column) & 1),
            None,
        )
        if selected is None:
            continue
        rows[pivot_row], rows[selected] = rows[selected], rows[pivot_row]
        right[pivot_row], right[selected] = right[selected], right[pivot_row]
        for row in range(count):
            if row != pivot_row and ((rows[row] >> column) & 1):
                rows[row] ^= rows[pivot_row]
                right[row] ^= right[pivot_row]
        pivot_columns.append(column)
        pivot_row += 1
        if pivot_row >= count:
            break
    for row in range(pivot_row, count):
        if rows[row] == 0 and right[row]:
            raise MiniAppBeastError("trial_lights_unsolvable")

    free_columns = [column for column in range(count) if column not in pivot_columns]
    best_solution = None
    for free_mask in range(1 << len(free_columns)):
        solution = [0] * count
        for offset, column in enumerate(free_columns):
            solution[column] = (free_mask >> offset) & 1
        solution_mask = sum((value << index) for index, value in enumerate(solution))
        for row, column in enumerate(pivot_columns):
            parity = (rows[row] & solution_mask).bit_count() % 2
            solution[column] = right[row] ^ parity
        if best_solution is None or sum(solution) < sum(best_solution):
            best_solution = solution
    solution = best_solution or [0] * count
    presses = [index for index, value in enumerate(solution) if value]
    final_cells = list(cells)
    for index in presses:
        for target_index in _trial_lights_neighbors(index, size):
            final_cells[target_index] ^= 1
    if any(value != target for value in final_cells):
        raise MiniAppBeastError("trial_lights_unsolved")
    duration = _trial_duration_ms(challenge, len(presses))
    step = max(80, duration // max(1, len(presses) + 1))
    return {
        "mode": "tianjiLightsOutV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"index": index, "t": step * (event_index + 1)}
            for event_index, index in enumerate(presses)
        ],
        "cells": final_cells,
    }


def _solve_memory(challenge: dict[str, Any]) -> dict[str, Any]:
    cards = [card for card in (challenge.get("cards") or []) if isinstance(card, dict)]
    pairs: dict[str, list[str]] = {}
    for card in cards:
        card_id = str(card.get("id") or "").strip()
        pair = str(card.get("pair") or "").strip()
        if card_id and pair:
            pairs.setdefault(pair, []).append(card_id)
    ordered = []
    for pair in pairs.values():
        if len(pair) != 2:
            raise MiniAppBeastError("trial_memory_invalid")
        ordered.extend(pair)
    if len(ordered) != len(cards) or not ordered:
        raise MiniAppBeastError("trial_memory_invalid")
    preview_ms = max(
        1800,
        int(challenge.get("previewMs") or challenge.get("preview_ms") or 3600),
    )
    first_event = preview_ms + 300
    step = 180
    duration = max(
        _trial_duration_ms(challenge, len(ordered)),
        first_event + step * max(0, len(ordered) - 1) + 450,
    )
    return {
        "mode": "tianjiMemoryV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"id": card_id, "index": index, "t": first_event + step * index}
            for index, card_id in enumerate(ordered)
        ],
        "mismatches": 0,
    }


def _solve_stargaze(challenge: dict[str, Any]) -> dict[str, Any]:
    stars = [star for star in (challenge.get("stars") or []) if isinstance(star, dict)]
    locked_ids = set(
        str(item)
        for item in (challenge.get("lockedNodeIds") or challenge.get("locked_node_ids") or [])
    )
    angles = {}
    moves = 0
    for star in stars:
        star_id = str(star.get("id") or "").strip()
        if not star_id:
            continue
        current = float(star.get("angle") or 0)
        target = float(star.get("targetAngle", star.get("target_angle", current)) or 0)
        locked = bool(star.get("locked")) or star_id in locked_ids
        angles[star_id] = (current if locked else target) % 360
        if not locked:
            moves += 1
    if not angles:
        raise MiniAppBeastError("trial_angles_invalid")
    return {
        "mode": "tianjiStargazeV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": _trial_duration_ms(challenge, moves),
        "angles": angles,
        "moves": moves,
        "misses": 0,
    }


def _solve_meridian(challenge: dict[str, Any]) -> dict[str, Any]:
    sequence = [str(item) for item in (challenge.get("sequence") or []) if str(item)]
    if not sequence:
        raise MiniAppBeastError("trial_sequence_invalid")
    preview_end = 620 + len(sequence) * 430
    first_event = preview_end + 300
    step = 200
    duration = max(
        _trial_duration_ms(challenge, len(sequence)),
        first_event + step * max(0, len(sequence) - 1) + 450,
    )
    return {
        "mode": "tianjiMeridianV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"id": point_id, "index": index, "t": first_event + step * index}
            for index, point_id in enumerate(sequence)
        ],
        "moves": len(sequence),
        "misses": 0,
    }


def _trial_orientation(
    left: dict[str, float],
    middle: dict[str, float],
    right: dict[str, float],
) -> float:
    return (middle["x"] - left["x"]) * (right["y"] - left["y"]) - (
        middle["y"] - left["y"]
    ) * (right["x"] - left["x"])


def _trial_point_on_segment(
    left: dict[str, float],
    middle: dict[str, float],
    right: dict[str, float],
) -> bool:
    epsilon = 0.000001
    return (
        min(left["x"], right["x"]) - epsilon
        <= middle["x"]
        <= max(left["x"], right["x"]) + epsilon
        and min(left["y"], right["y"]) - epsilon
        <= middle["y"]
        <= max(left["y"], right["y"]) + epsilon
        and abs(_trial_orientation(left, right, middle)) <= epsilon
    )


def _trial_segments_cross(
    first_left: dict[str, float],
    first_right: dict[str, float],
    second_left: dict[str, float],
    second_right: dict[str, float],
) -> bool:
    """Mirror the Mini App's crossing test for non-adjacent graph edges."""
    epsilon = 0.000001
    first_a = _trial_orientation(first_left, first_right, second_left)
    first_b = _trial_orientation(first_left, first_right, second_right)
    second_a = _trial_orientation(second_left, second_right, first_left)
    second_b = _trial_orientation(second_left, second_right, first_right)
    if first_a * first_b < -epsilon and second_a * second_b < -epsilon:
        return True
    return bool(
        abs(first_a) <= epsilon
        and _trial_point_on_segment(first_left, second_left, first_right)
        or abs(first_b) <= epsilon
        and _trial_point_on_segment(first_left, second_right, first_right)
        or abs(second_a) <= epsilon
        and _trial_point_on_segment(second_left, first_left, second_right)
        or abs(second_b) <= epsilon
        and _trial_point_on_segment(second_left, first_right, second_right)
    )


def _trial_crossing_count(
    edges: list[tuple[str, str]],
    positions: dict[str, dict[str, float]],
) -> int:
    count = 0
    for index, (first_left, first_right) in enumerate(edges):
        if first_left not in positions or first_right not in positions:
            return len(edges) + 1
        for second_left, second_right in edges[index + 1 :]:
            if {first_left, first_right} & {second_left, second_right}:
                continue
            if second_left not in positions or second_right not in positions:
                return len(edges) + 1
            if _trial_segments_cross(
                positions[first_left],
                positions[first_right],
                positions[second_left],
                positions[second_right],
            ):
                count += 1
    return count


def _trial_minimum_node_distance(positions: dict[str, dict[str, float]]) -> float:
    points = list(positions.values())
    minimum = float("inf")
    for index, left in enumerate(points):
        for right in points[index + 1 :]:
            minimum = min(
                minimum,
                math.hypot(left["x"] - right["x"], left["y"] - right["y"]),
            )
    return minimum


def _trial_layout_valid(
    edges: list[tuple[str, str]],
    positions: dict[str, dict[str, float]],
    *,
    minimum_distance: float = 4.0,
) -> bool:
    return bool(
        positions
        and all(
            math.isfinite(point["x"])
            and math.isfinite(point["y"])
            and 4.0 <= point["x"] <= 96.0
            and 4.0 <= point["y"] <= 96.0
            for point in positions.values()
        )
        and _trial_crossing_count(edges, positions) == 0
        and _trial_minimum_node_distance(positions) >= minimum_distance
    )


def _trial_fixed_planar_layout(
    raw_layout: dict[str, Any],
    locked_positions: dict[str, dict[str, float]],
    edges: list[tuple[str, str]],
) -> dict[str, dict[str, float]] | None:
    """Fit a straight-line planar drawing onto one fixed trial node."""
    if len(locked_positions) != 1:
        return None
    locked_id, locked_point = next(iter(locked_positions.items()))
    if locked_id not in raw_layout:
        return None
    base_x, base_y = raw_layout[locked_id]
    vectors = {
        node_id: (float(point[0]) - float(base_x), float(point[1]) - float(base_y))
        for node_id, point in raw_layout.items()
    }
    best: tuple[float, dict[str, dict[str, float]]] | None = None
    for degree in range(0, 360, 5):
        radians = math.radians(degree)
        cosine = math.cos(radians)
        sine = math.sin(radians)
        rotated = {
            node_id: (
                cosine * vector[0] - sine * vector[1],
                sine * vector[0] + cosine * vector[1],
            )
            for node_id, vector in vectors.items()
        }
        scale_limits = []
        for delta_x, delta_y in rotated.values():
            if delta_x > 0:
                scale_limits.append((96.0 - locked_point["x"]) / delta_x)
            elif delta_x < 0:
                scale_limits.append((locked_point["x"] - 4.0) / -delta_x)
            if delta_y > 0:
                scale_limits.append((96.0 - locked_point["y"]) / delta_y)
            elif delta_y < 0:
                scale_limits.append((locked_point["y"] - 4.0) / -delta_y)
        positive_limits = [limit for limit in scale_limits if limit > 0]
        if not positive_limits:
            continue
        scale = min(positive_limits) * 0.98
        positions = {
            node_id: {
                "x": locked_point["x"] + delta_x * scale,
                "y": locked_point["y"] + delta_y * scale,
            }
            for node_id, (delta_x, delta_y) in rotated.items()
        }
        positions[locked_id] = dict(locked_point)
        if not _trial_layout_valid(edges, positions):
            continue
        score = _trial_minimum_node_distance(positions)
        if best is None or score > best[0]:
            best = (score, positions)
    return best[1] if best else None


def _solve_planarity(challenge: dict[str, Any]) -> dict[str, Any]:
    nodes = [node for node in (challenge.get("nodes") or []) if isinstance(node, dict)]
    edges = [edge for edge in (challenge.get("edges") or []) if isinstance(edge, dict)]
    graph = nx.Graph()
    node_by_id = {}
    for node in nodes:
        node_id = str(node.get("id") or "").strip()
        if node_id:
            graph.add_node(node_id)
            node_by_id[node_id] = node
    graph_edges = []
    for edge in edges:
        left = str(edge.get("from") or "").strip()
        right = str(edge.get("to") or "").strip()
        if left in graph and right in graph and left != right:
            graph.add_edge(left, right)
            normalized = tuple(sorted((left, right)))
            if normalized not in graph_edges:
                graph_edges.append(normalized)
    planar, embedding = nx.check_planarity(graph)
    if not planar or not graph.nodes:
        raise MiniAppBeastError("trial_planarity_invalid")
    locked = set(
        str(item)
        for item in (challenge.get("lockedNodeIds") or challenge.get("locked_node_ids") or [])
    )
    locked.update(
        str(node.get("id") or "") for node in nodes if bool(node.get("locked"))
    )

    def scaled_layout(raw_layout: dict[str, Any]) -> dict[str, dict[str, float]]:
        xs = [float(point[0]) for point in raw_layout.values()]
        ys = [float(point[1]) for point in raw_layout.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(1.0, max_x - min_x)
        span_y = max(1.0, max_y - min_y)
        return {
            node_id: {
                "x": 8.0 + (float(point[0]) - min_x) * 84.0 / span_x,
                "y": 8.0 + (float(point[1]) - min_y) * 84.0 / span_y,
            }
            for node_id, point in raw_layout.items()
        }

    raw_layout = nx.combinatorial_embedding_to_pos(embedding)
    locked_positions = {
        node_id: {
            "x": float(
                50
                if node_by_id[node_id].get("x") is None
                else node_by_id[node_id].get("x")
            ),
            "y": float(
                50
                if node_by_id[node_id].get("y") is None
                else node_by_id[node_id].get("y")
            ),
        }
        for node_id in locked
        if node_id in node_by_id
    }
    positions = scaled_layout(raw_layout)
    fixed_layout = _trial_fixed_planar_layout(
        raw_layout,
        locked_positions,
        graph_edges,
    )
    if fixed_layout is not None:
        positions = fixed_layout
    elif locked:
        # Locked trial nodes must keep their exact coordinates.  Replacing the
        # locked set with one helper vertex preserves all other adjacencies and
        # lets the planar embedding place movable nodes around that fixed core.
        helper = "__locked_trial_core__"
        reduced = graph.copy()
        locked_neighbors = set()
        for node_id in locked:
            if node_id in reduced:
                locked_neighbors.update(reduced.neighbors(node_id))
        reduced.remove_nodes_from(locked)
        reduced.add_node(helper)
        reduced.add_edges_from(
            (helper, neighbor)
            for neighbor in locked_neighbors
            if neighbor in reduced and neighbor != helper
        )
        reduced_planar, reduced_embedding = nx.check_planarity(reduced)
        if not reduced_planar:
            raise MiniAppBeastError("trial_planarity_invalid")
        positions.update(
            {
                node_id: point
                for node_id, point in scaled_layout(
                    nx.combinatorial_embedding_to_pos(reduced_embedding)
                ).items()
                if node_id != helper
            }
        )
        positions.update(locked_positions)
    if not _trial_layout_valid(graph_edges, positions):
        raise MiniAppBeastError("trial_planarity_unsolved")
    moves = sum(
        1
        for node in nodes
        if str(node.get("id") or "") not in locked and not node.get("locked")
    )
    return {
        "mode": "tianjiPlanarityV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": _trial_duration_ms(challenge, moves),
        "positions": positions,
        "moves": moves,
        "misses": 0,
    }


def solve_tianji_trial_challenge(challenge: Any) -> dict[str, Any]:
    if not isinstance(challenge, dict) or not challenge.get("challengeId"):
        raise MiniAppBeastError("trial_challenge_missing")
    mode = str(challenge.get("mode") or "")
    solvers = {
        "tianjiLightsOutV1": _solve_lights_out,
        "tianjiMemoryV1": _solve_memory,
        "tianjiStargazeV1": _solve_stargaze,
        "tianjiMeridianV1": _solve_meridian,
        "tianjiPlanarityV1": _solve_planarity,
    }
    solver = solvers.get(mode)
    if solver is None:
        raise MiniAppBeastError("trial_mode_unsupported")
    return solver(challenge)


def _cell_direction(from_index: int, to_index: int, size: int) -> str:
    from_row, from_col = divmod(from_index, size)
    to_row, to_col = divmod(to_index, size)
    vertical = "北" if to_row < from_row else "南" if to_row > from_row else ""
    horizontal = "西" if to_col < from_col else "东" if to_col > from_col else ""
    return vertical + horizontal or "此地"


def _hunt_hint_data(run: dict[str, Any]) -> tuple[dict[int, str], list[tuple[int, str]]]:
    markers: dict[int, str] = {}
    constraints: list[tuple[int, str]] = []
    cells = run.get("cells") if isinstance(run.get("cells"), list) else []
    for cell in cells:
        if not isinstance(cell, dict) or not cell.get("revealed"):
            continue
        hint = cell.get("hint") if isinstance(cell.get("hint"), dict) else {}
        if not hint:
            continue
        try:
            source_index = int(cell.get("index"))
        except (TypeError, ValueError):
            continue
        match = HUNT_DIRECTION_RE.search(str(hint.get("text") or ""))
        if match and match.group(1) != "此地":
            direction = HUNT_DIRECTION_ALIASES.get(match.group(1), match.group(1))
            constraints.append((source_index, direction))
        for marker in hint.get("markers") or []:
            if not isinstance(marker, dict):
                continue
            try:
                marker_index = int(marker.get("index"))
            except (TypeError, ValueError):
                continue
            kind = str(marker.get("kind") or "").strip().casefold()
            if kind in {"risk", "treasure", "resource"}:
                markers[marker_index] = kind
    return markers, constraints


def choose_hunt_cell(run: Any, rng: Any = None) -> int | None:
    """Choose the next cell, prioritizing yellow treasure hints and avoiding risk."""
    if not isinstance(run, dict) or int(run.get("ap") or 0) <= 0:
        return None
    cells = run.get("cells") if isinstance(run.get("cells"), list) else []
    available = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("revealed"):
            continue
        try:
            available.append(int(cell.get("index")))
        except (TypeError, ValueError):
            continue
    if not available:
        return None

    size = max(1, int(run.get("size") or 5))
    markers, constraints = _hunt_hint_data(run)
    constrained = [
        index
        for index in available
        if all(_cell_direction(source, index, size) == direction for source, direction in constraints)
    ]
    if constraints and not constrained:
        constrained = list(available)

    treasure = [index for index in available if markers.get(index) == "treasure"]
    treasure_constrained = [index for index in treasure if index in constrained]
    safe = [index for index in available if markers.get(index) not in {"risk", "resource"}]
    safe_constrained = [index for index in constrained if index in safe]
    unmarked_safe = [index for index in safe if index not in markers]

    if treasure_constrained:
        pool = treasure_constrained
    elif treasure:
        pool = treasure
    elif constraints and safe_constrained:
        pool = safe_constrained
    elif markers and unmarked_safe:
        pool = unmarked_safe
    elif safe:
        pool = safe
    else:
        pool = available
    chooser = rng or random.SystemRandom()
    return int(chooser.choice(sorted(pool)))


class MiniAppDailyActivities:
    """Run daily pagoda and treasure-hunt actions for every mapped identity."""

    def __init__(
        self,
        actor: Any,
        transport: Any,
        account: str,
        logger: Any,
    ) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        config = getattr(actor, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        default_minute = ACCOUNT_MINUTE_OFFSETS.get(self.account, 0)
        self.pagoda_enabled = bool(settings.get("pagoda_daily_enabled", True))
        self.hunt_enabled = bool(settings.get("hunt_daily_enabled", True))
        self.tianji_trial_hour = _bounded_int(
            settings.get("tianji_trial_daily_hour"), DEFAULT_TIANJI_TRIAL_HOUR, 0, 23
        )
        self.tianji_trial_minute = _bounded_int(
            settings.get("tianji_trial_daily_minute"), default_minute, 0, 59
        )
        self.fate_cards_hour = _bounded_int(
            settings.get("fate_cards_daily_hour"), DEFAULT_FATE_CARDS_HOUR, 0, 23
        )
        self.fate_cards_minute = _bounded_int(
            settings.get("fate_cards_daily_minute"), default_minute, 0, 59
        )
        self.pagoda_hour = _bounded_int(
            settings.get("pagoda_daily_hour"), DEFAULT_PAGODA_HOUR, 0, 23
        )
        self.pagoda_minute = _bounded_int(
            settings.get("pagoda_daily_minute"), default_minute, 0, 59
        )
        self.hunt_hour = _bounded_int(
            settings.get("hunt_daily_hour"), DEFAULT_HUNT_HOUR, 0, 23
        )
        self.hunt_minute = _bounded_int(
            settings.get("hunt_daily_minute"), default_minute, 0, 59
        )
        self.retry_seconds = max(
            60,
            int(settings.get("miniapp_daily_retry_seconds") or DEFAULT_DAILY_RETRY_SECONDS),
        )
        self.target_reward = str(
            settings.get("hunt_target_reward") or DEFAULT_TARGET_REWARD
        ).strip()
        self.rng = random.SystemRandom()

    def identities(self) -> list[str]:
        ids = getattr(self.transport, "identity_player_ids", {}) or {}
        result = []
        for identity in ["主魂", *(getattr(self.actor, "avatars", []) or [])]:
            key = str(identity or "主魂").strip() or "主魂"
            if key in ids or key.casefold() in ids:
                result.append(key)
        return result

    def tianji_trial_identities(self) -> list[str]:
        settings = load_automation_settings()
        trial = miniapp_tianji_trial_settings(settings)
        if not bool(trial.get("enabled", True)):
            return []
        selected = set(miniapp_tianji_trial_identities_for_account(self.account, settings))
        return [identity for identity in self.identities() if identity in selected]

    def fate_cards_identities(self) -> list[str]:
        settings = load_automation_settings()
        fate = miniapp_fate_cards_settings(settings)
        if not bool(fate.get("enabled", True)):
            return []
        selected = set(miniapp_fate_cards_identities_for_account(self.account, settings))
        return [identity for identity in self.identities() if identity in selected]

    @property
    def tianji_trial_enabled(self) -> bool:
        return bool(miniapp_tianji_trial_settings().get("enabled", True))

    @property
    def fate_cards_enabled(self) -> bool:
        return bool(miniapp_fate_cards_settings().get("enabled", True))

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App daily activity state save failed", exc_info=True)

    def _state(self, identity: str) -> dict[str, Any]:
        return identity_state(self.actor, identity)

    def _record(self, identity: str, **updates: Any) -> None:
        self._state(identity).update(updates)
        self._save()

    def _record_next_schedule(self, feature: str, target: datetime) -> None:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            return
        state[f"miniapp_{feature}_next_run_time"] = target.strftime(TIME_FORMAT)
        self._save()

    def _identity_pause_seconds(self, identity: str) -> int:
        resolver = getattr(self.actor, "identity_pause_seconds", None)
        if not callable(resolver):
            return 0
        try:
            return max(0, int(resolver(identity) or 0))
        except Exception:
            return 0

    async def _wait_until_runnable(self) -> None:
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        pause = getattr(self.actor, "pause_event", None)
        if pause is not None:
            await pause.wait()

    def _record_error(self, identity: str, feature: str, exc: Exception) -> None:
        code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
        now = datetime.now().strftime(TIME_FORMAT)
        self._record(
            identity,
            **{
                f"miniapp_{feature}_last_error": code,
                f"miniapp_{feature}_last_error_time": now,
            },
        )
        if isinstance(exc, MiniAppCircuitOpenError):
            self.log.info(
                "Mini App %s paused for %s while upstream circuit is open; next probe %s",
                feature,
                identity,
                exc.retry_at or f"in {exc.retry_after}s",
            )
        else:
            self.log.error(
                "Mini App %s failed for %s: %s",
                feature,
                identity,
                code,
                exc_info=True,
            )

    def _hunt_summary_loot(self, identity: str, today: str) -> dict[str, int]:
        state = self._state(identity)
        if state.get("miniapp_hunt_summary_date") != today:
            state["miniapp_hunt_summary_date"] = today
            state["miniapp_hunt_summary_loot"] = {}
            state["miniapp_hunt_summary_completed"] = 0
            state["miniapp_hunt_summary_logged_date"] = ""
            state["miniapp_hunt_last_result"] = ""
            self._save()
        return normalize_hunt_loot(state.get("miniapp_hunt_summary_loot"))

    def _log_hunt_summary(
        self,
        identity: str,
        today: str,
        counter: dict[str, int],
    ) -> str:
        state = self._state(identity)
        summary = hunt_loot_text(state.get("miniapp_hunt_summary_loot"))
        if state.get("miniapp_hunt_summary_logged_date") == today:
            return summary
        limit = int(counter.get("limit") or 3)
        operation = f"洞府寻宝（每日 {limit} 局）"
        self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
        self.log.info("IN [Mini App | %s]:\n%s -> %s", identity, operation, summary)
        self._record(
            identity,
            miniapp_hunt_summary_logged_date=today,
            miniapp_hunt_last_result=summary,
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder):
            recorder(
                identity,
                ".洞府寻宝",
                f"三局总获得：{summary}",
                source="Mini App 洞府寻宝",
                final=True,
            )
        return summary

    async def run_pagoda_identity(self, identity: str, today: str | None = None) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_pagoda_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        snapshot = await self.transport.pagoda_snapshot(identity)
        pagoda = snapshot.get("state") if isinstance(snapshot, dict) else {}
        pagoda = pagoda if isinstance(pagoda, dict) else {}
        attempted = bool(
            int(pagoda.get("todayHighest") or 0) > 0
            or int(pagoda.get("failedFloor") or 0) > 0
            or int(pagoda.get("resetsToday") or 0) > 0
        )
        if attempted:
            now = datetime.now().strftime(TIME_FORMAT)
            self._record(
                identity,
                miniapp_pagoda_last_date=today,
                miniapp_pagoda_last_time=now,
                miniapp_pagoda_last_result="服务器已记录今日登塔",
                miniapp_pagoda_last_error="",
                last_tower_date=today,
            )
            return "already"
        if not pagoda.get("canChallenge"):
            self._record(
                identity,
                miniapp_pagoda_last_error="pagoda_not_ready",
                miniapp_pagoda_last_error_time=datetime.now().strftime(TIME_FORMAT),
            )
            return "retry"

        payload = await self.transport.pagoda_challenge(identity)
        text = pagoda_result_text(payload)
        now = datetime.now().strftime(TIME_FORMAT)
        replay = payload.get("replay") if isinstance(payload, dict) else {}
        replay = replay if isinstance(replay, dict) else {}
        self._record(
            identity,
            miniapp_pagoda_last_date=today,
            miniapp_pagoda_last_time=now,
            miniapp_pagoda_last_result=text[:1000],
            miniapp_pagoda_last_end_floor=int(replay.get("endFloor") or 0),
            miniapp_pagoda_last_failed_floor=int(replay.get("failedFloor") or 0),
            miniapp_pagoda_last_error="",
            last_tower_date=today,
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and text:
            recorder(identity, ".闯塔", text, source="Mini App 琉璃问心塔", final=True)
        return "challenged"

    async def run_pagoda_daily_once(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        identities = self.identities()
        if all(
            self._state(identity).get("miniapp_pagoda_last_date") == today
            for identity in identities
        ):
            return True
        circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
        if circuit_error is not None:
            raise circuit_error
        await self.transport.initialize()
        complete = True
        for identity in identities:
            try:
                result = await self.run_pagoda_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "pagoda", exc)
            await asyncio.sleep(1)
        return complete

    async def run_tianji_trial_identity(
        self,
        identity: str,
        today: str | None = None,
    ) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_tianji_trial_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        try:
            payload = await self.transport.tianji_trial_start(identity)
        except MiniAppBeastError as exc:
            if exc.code != "trial_daily_limit":
                raise
            self._record(
                identity,
                miniapp_tianji_trial_last_date=today,
                miniapp_tianji_trial_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_tianji_trial_completed=3,
                miniapp_tianji_trial_limit=3,
                miniapp_tianji_trial_last_result="今日 3 关已完成",
                miniapp_tianji_trial_last_error="",
            )
            return "already"
        completed, limit = tianji_trial_progress(payload)
        if completed >= limit:
            self._record(
                identity,
                miniapp_tianji_trial_last_date=today,
                miniapp_tianji_trial_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_tianji_trial_completed=completed,
                miniapp_tianji_trial_limit=limit,
                miniapp_tianji_trial_last_result="今日 3 关已完成",
                miniapp_tianji_trial_last_error="",
            )
            return "already"
        if not payload.get("challenge"):
            raise MiniAppBeastError("trial_challenge_missing")

        results = []
        challenge = payload.get("challenge")
        for _ in range(max(1, limit - completed)):
            proof = solve_tianji_trial_challenge(challenge)
            await asyncio.sleep((int(proof.get("durationMs") or 0) + 500) / 1000)
            settled = await self.transport.tianji_trial_finish(identity, proof)
            results.append(settled)
            completed, limit = tianji_trial_progress(settled)
            challenge = settled.get("nextChallenge")
            self._record(
                identity,
                miniapp_tianji_trial_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_tianji_trial_completed=completed,
                miniapp_tianji_trial_limit=limit,
                miniapp_tianji_trial_last_error="",
            )
            if completed >= limit or not challenge:
                break

        if completed < limit:
            return "retry"
        summary = tianji_trial_result_text(results)
        operation = f"天机试炼（每日 {limit} 关）"
        self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
        self.log.info(
            "IN [Mini App | %s]:\n%s ->\n%s",
            identity,
            operation,
            tianji_trial_log_text(results),
        )
        self._record(
            identity,
            miniapp_tianji_trial_last_date=today,
            miniapp_tianji_trial_last_time=datetime.now().strftime(TIME_FORMAT),
            miniapp_tianji_trial_completed=completed,
            miniapp_tianji_trial_limit=limit,
            miniapp_tianji_trial_last_result=summary,
            miniapp_tianji_trial_last_error="",
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder):
            recorder(
                identity,
                ".天机试炼",
                summary,
                source="Mini App 天机试炼",
                final=True,
            )
        return "completed"

    async def run_tianji_trial_daily_once(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        identities = self.tianji_trial_identities()
        if all(
            self._state(identity).get("miniapp_tianji_trial_last_date") == today
            for identity in identities
        ):
            return True
        circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
        if circuit_error is not None:
            raise circuit_error
        await self.transport.initialize()
        complete = True
        for identity in identities:
            try:
                result = await self.run_tianji_trial_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "tianji_trial", exc)
            await asyncio.sleep(1)
        return complete

    async def _prepare_fate_cards_accept(self, identity: str, today: str) -> bool:
        force_completed = False
        try:
            forced = await self.transport.deep_seclusion_action(
                identity,
                "force",
                log_operation=False,
            )
            apply_dwelling_snapshot(self.actor, identity, forced)
            action_result = (
                forced.get("actionResult")
                if isinstance(forced, dict) and isinstance(forced.get("actionResult"), dict)
                else {}
            )
            force_completed = action_result.get("ok") is not False
        except MiniAppBeastError as exc:
            if exc.code != "deep_not_active":
                raise
        if not force_completed:
            cultivated = await self.transport.command(
                ".闭关修炼",
                identity=identity,
                log_operation=False,
            )
            apply_dwelling_snapshot(self.actor, identity, cultivated.payload)
            if not command_result_ok(cultivated.payload):
                return False
            self._record(
                identity,
                miniapp_fate_cards_accept_prepared_date=today,
                miniapp_fate_cards_stage="accept_cultivated",
            )
            return True
        try:
            started = await self.transport.deep_seclusion_action(
                identity,
                "start",
                log_operation=False,
            )
            apply_dwelling_snapshot(self.actor, identity, started)
        except Exception:
            if force_completed:
                self._record(
                    identity,
                    miniapp_fate_cards_accept_prepared_date=today,
                    miniapp_fate_cards_stage="accept_restart_pending",
                )
            raise
        self._record(
            identity,
            miniapp_fate_cards_accept_prepared_date=today,
            miniapp_fate_cards_stage="accept_prepared",
        )
        return True

    def _finish_fate_cards(
        self,
        identity: str,
        today: str,
        payload: dict[str, Any],
        record: dict[str, Any],
        *,
        result: str,
    ) -> str:
        summary = fate_cards_result_text(payload, record)
        choice = str(record.get("choiceKey") or "").strip()
        operation = "天机命脉（问天·机缘）"
        self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
        self.log.info(
            "IN [Mini App | %s]:\n%s ->\n%s",
            identity,
            operation,
            summary,
        )
        self._record(
            identity,
            miniapp_fate_cards_last_date=today,
            miniapp_fate_cards_last_time=datetime.now().strftime(TIME_FORMAT),
            miniapp_fate_cards_last_choice=choice,
            miniapp_fate_cards_last_result=summary,
            miniapp_fate_cards_last_error="",
            miniapp_fate_cards_stage="settled",
            miniapp_fate_cards_next_run_time="",
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder):
            recorder(
                identity,
                ".天机命脉",
                summary,
                source="Mini App 天机命脉",
                final=True,
            )
        return result

    async def run_fate_cards_identity(
        self,
        identity: str,
        today: str | None = None,
    ) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_fate_cards_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        payload = await self.transport.fate_cards_start(identity)
        record = fate_cards_record(payload)
        quest = fate_cards_quest(record)
        if record and str(quest.get("status") or "") == "settled":
            return self._finish_fate_cards(
                identity,
                today,
                payload,
                record,
                result="already",
            )

        if not record:
            payload = await self.transport.fate_cards_draw(identity, "opportunity")
            record = fate_cards_record(payload)
            if not record:
                raise MiniAppBeastError("fate_cards_record_missing")
            self._record(
                identity,
                miniapp_fate_cards_stage="drawn",
                miniapp_fate_cards_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_fate_cards_last_error="",
            )
            for _ in record.get("cards") or range(3):
                await asyncio.sleep(0.38)

        if not fate_cards_ai_ready(record):
            payload = await self.transport.fate_cards_interpret(identity)
            record = fate_cards_record(payload)
            if not fate_cards_ai_ready(record):
                raise MiniAppBeastError("fate_cards_interpretation_missing")
            self._record(
                identity,
                miniapp_fate_cards_stage="interpreted",
                miniapp_fate_cards_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_fate_cards_last_error="",
            )

        choice = str(record.get("choiceKey") or "").strip()
        if not choice:
            choice = str(self.rng.choice(("accept", "hide")))
            payload = await self.transport.fate_cards_choose(identity, choice)
            record = fate_cards_record(payload)
            if str(record.get("choiceKey") or "").strip() != choice:
                raise MiniAppBeastError("fate_cards_choice_missing")
            self._record(
                identity,
                miniapp_fate_cards_stage="chosen",
                miniapp_fate_cards_last_choice=choice,
                miniapp_fate_cards_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_fate_cards_last_error="",
            )
        if choice not in FATE_CARD_CHOICE_NAMES:
            raise MiniAppBeastError("fate_cards_existing_choice_unsupported")

        quest = fate_cards_quest(record)
        if choice == "hide" and not quest.get("canSettle"):
            wait = fate_cards_wait_seconds(quest)
            if wait > 0:
                self._record(
                    identity,
                    miniapp_fate_cards_stage="waiting_hide",
                    miniapp_fate_cards_next_run_time=(
                        datetime.now() + timedelta(seconds=wait + 1)
                    ).strftime(TIME_FORMAT),
                )
                await asyncio.sleep(wait + 1)
            payload = await self.transport.fate_cards_start(identity)
            record = fate_cards_record(payload)
            quest = fate_cards_quest(record)
        elif choice == "accept" and not quest.get("canSettle"):
            await self._prepare_fate_cards_accept(identity, today)
            await asyncio.sleep(1)
            payload = await self.transport.fate_cards_start(identity)
            record = fate_cards_record(payload)
            quest = fate_cards_quest(record)

        if str(quest.get("status") or "") == "settled":
            return self._finish_fate_cards(
                identity,
                today,
                payload,
                record,
                result="already",
            )
        if not quest.get("canSettle"):
            self._record(
                identity,
                miniapp_fate_cards_stage="quest_pending",
                miniapp_fate_cards_last_result=(
                    f"{FATE_CARD_CHOICE_NAMES[choice]}："
                    f"{int(quest.get('progress') or 0)} / {int(quest.get('target') or 0)} "
                    f"{str(quest.get('unit') or '').strip()}"
                ).strip(),
                miniapp_fate_cards_next_run_time=(
                    datetime.now() + timedelta(seconds=self.retry_seconds)
                ).strftime(TIME_FORMAT),
            )
            return "retry"

        settled = await self.transport.fate_cards_settle(identity)
        settled_record = fate_cards_record(settled)
        settled_quest = fate_cards_quest(settled_record)
        if (
            str(settled_quest.get("status") or "") != "settled"
            and not settled.get("alreadySettled")
        ):
            raise MiniAppBeastError("fate_cards_settlement_missing")
        return self._finish_fate_cards(
            identity,
            today,
            settled,
            settled_record,
            result="completed",
        )

    async def run_fate_cards_daily_once(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        identities = self.fate_cards_identities()
        if all(
            self._state(identity).get("miniapp_fate_cards_last_date") == today
            for identity in identities
        ):
            return True
        circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
        if circuit_error is not None:
            raise circuit_error
        await self.transport.initialize()
        complete = True
        for identity in identities:
            try:
                result = await self.run_fate_cards_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "fate_cards", exc)
            await asyncio.sleep(1)
        return complete

    async def play_hunt_session(
        self,
        identity: str,
        run: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        reason = "status_closed"
        for _ in range(25):
            if hunt_loot_contains(run, self.target_reward):
                return run, "target_reward"
            if run.get("foundMain"):
                return run, "main_found"
            if str(run.get("status") or "active") != "active":
                return run, "status_closed"
            if int(run.get("ap") or 0) <= 0:
                return run, "ap_depleted"
            index = choose_hunt_cell(run, rng=self.rng)
            if index is None:
                return run, "no_cell"
            payload = await self.transport.hunt_reveal(
                identity,
                str(run.get("sessionId") or ""),
                index,
            )
            next_run = hunt_run(payload)
            if not next_run:
                raise MiniAppBeastError("hunt_session_missing")
            run = next_run
            self._record(
                identity,
                miniapp_hunt_last_action="reveal",
                miniapp_hunt_last_action_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_last_cell=index,
                miniapp_hunt_last_ap=int(run.get("ap") or 0),
                miniapp_hunt_last_error="",
            )
            await asyncio.sleep(0.2)
        return run, reason

    async def run_hunt_identity(self, identity: str, today: str | None = None) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_hunt_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        snapshot = await self.transport.hunt_snapshot(identity)
        dwelling = snapshot.get("dwelling") if isinstance(snapshot, dict) else {}
        dwelling = dwelling if isinstance(dwelling, dict) else {}
        if dwelling.get("hasDwelling") is False:
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_result="当前身份未开辟专属洞府",
                miniapp_hunt_last_error="dwelling_missing",
            )
            return "unavailable"

        counter = hunt_counter(snapshot)
        run = hunt_run(snapshot)
        summary_loot = self._hunt_summary_loot(identity, today)
        completed = 0
        if counter["remaining"] <= 0 and not run:
            if summary_loot:
                self._log_hunt_summary(identity, today, counter)
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_result=(
                    hunt_loot_text(summary_loot) if summary_loot else "今日寻宝次数已用完"
                ),
                miniapp_hunt_last_error="",
            )
            return "already"

        safety_limit = max(1, min(3, counter["remaining"] + (1 if run else 0)))
        while (run or counter["remaining"] > 0) and completed < safety_limit:
            if not run:
                started = await self.transport.hunt_start(identity)
                run = hunt_run(started)
                started_counter = hunt_counter(started)
                if started_counter["limit"] > 0:
                    counter = started_counter
                if not run:
                    raise MiniAppBeastError("hunt_session_missing")
            run, reason = await self.play_hunt_session(identity, run)
            session_id = str(run.get("sessionId") or "").strip()
            settled = await self.transport.hunt_settle(identity, session_id)
            summary_loot = merge_hunt_loot(summary_loot, settled)
            settled_counter = hunt_counter(settled)
            if settled_counter["limit"] <= 0:
                settled_counter = hunt_counter(await self.transport.hunt_snapshot(identity))
            counter = settled_counter
            completed += 1
            self._record(
                identity,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_last_stop_reason=reason,
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_error="",
                miniapp_hunt_summary_date=today,
                miniapp_hunt_summary_loot=summary_loot,
                miniapp_hunt_summary_completed=int(
                    self._state(identity).get("miniapp_hunt_summary_completed") or 0
                ) + 1,
            )
            run = {}
            await asyncio.sleep(1)

        if counter["remaining"] <= 0:
            summary = self._log_hunt_summary(identity, today, counter)
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_result=summary,
                miniapp_hunt_last_error="",
            )
            return "completed"
        return "retry"

    async def run_hunt_daily_once(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        identities = self.identities()
        if all(
            self._state(identity).get("miniapp_hunt_last_date") == today
            for identity in identities
        ):
            return True
        circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
        if circuit_error is not None:
            raise circuit_error
        await self.transport.initialize()
        complete = True
        for identity in identities:
            try:
                result = await self.run_hunt_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "hunt", exc)
            await asyncio.sleep(1)
        return complete

    @staticmethod
    def _target_datetime(now: datetime, hour: int, minute: int) -> datetime:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _run_daily_loop(self, feature: str, hour: int, minute: int, callback: Any) -> None:
        await self._wait_until_runnable()
        while getattr(self.actor, "is_running", True):
            now = datetime.now()
            target = self._target_datetime(now, hour, minute)
            if now < target:
                self._record_next_schedule(feature, target)
                await asyncio.sleep(max(60, int((target - now).total_seconds())))
                continue
            pause = getattr(self.actor, "pause_event", None)
            if pause is not None:
                await pause.wait()
            circuit_wait = 0
            try:
                complete = await callback(now=now)
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError as exc:
                circuit_wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
                self.log.info(
                    "Mini App %s daily cycle paused by upstream circuit until %s",
                    feature,
                    exc.retry_at or f"in {circuit_wait}s",
                )
                complete = False
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self.log.error(
                    "Mini App %s daily cycle failed before identity processing: %s",
                    feature,
                    code,
                    exc_info=True,
                )
                complete = False
            if complete:
                next_target = target + timedelta(days=1)
                wait = max(60, int((next_target - datetime.now()).total_seconds()))
                self._record_next_schedule(feature, next_target)
            else:
                wait = circuit_wait or self.retry_seconds
                self._record_next_schedule(
                    feature,
                    datetime.now() + timedelta(seconds=wait),
                )
            self.log.info(
                "Mini App %s daily cycle %s; next check in %ss",
                feature,
                "complete" if complete else "needs retry",
                wait,
            )
            await asyncio.sleep(wait)

    async def run_pagoda_loop(self) -> None:
        if not self.pagoda_enabled:
            return
        await self._run_daily_loop(
            "pagoda",
            self.pagoda_hour,
            self.pagoda_minute,
            self.run_pagoda_daily_once,
        )

    async def run_hunt_loop(self) -> None:
        if not self.hunt_enabled:
            return
        await self._run_daily_loop(
            "hunt",
            self.hunt_hour,
            self.hunt_minute,
            self.run_hunt_daily_once,
        )

    async def run_tianji_trial_loop(self) -> None:
        await self._run_daily_loop(
            "tianji_trial",
            self.tianji_trial_hour,
            self.tianji_trial_minute,
            self.run_tianji_trial_daily_once,
        )

    async def run_fate_cards_loop(self) -> None:
        await self._run_daily_loop(
            "fate_cards",
            self.fate_cards_hour,
            self.fate_cards_minute,
            self.run_fate_cards_daily_once,
        )
