"""Automate the Telegram desktop 2048 mini app with an expectimax player."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import json
import math
import random
import sys
import time
from functools import lru_cache
from pathlib import Path

from PIL import ImageGrab


WINDOW_TITLE = "韩天尊"
CELL_X = (320, 416, 512, 608)
CELL_Y = (315, 412, 509, 606)
SUBMIT_XY = (575, 249)

# Sampled from the mini app theme. Coordinates intentionally avoid tile text.
TILE_COLORS = {
    0: (30, 53, 44),
    2: (53, 74, 66),
    4: (73, 80, 66),
    8: (49, 97, 82),
    32: (108, 79, 58),
    128: (54, 95, 103),
    256: (85, 74, 109),
}

DIRECTIONS = ("left", "up", "right", "down")
KEYS = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def classify_color(rgb: tuple[int, int, int]) -> int:
    return min(TILE_COLORS, key=lambda value: color_distance(rgb, TILE_COLORS[value]))


def capture_classes() -> tuple[int, ...]:
    image = ImageGrab.grab(all_screens=True).convert("RGB")
    return tuple(classify_color(image.getpixel((x, y))) for y in CELL_Y for x in CELL_X)


def calibrate_board(hwnd: int) -> None:
    global CELL_X, CELL_Y, SUBMIT_XY

    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    image = ImageGrab.grab(all_screens=True).convert("RGB")
    buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
    target = TILE_COLORS[256]
    for y in range(max(rect.top + 100, 0), min(rect.bottom - 80, image.height), 2):
        for x in range(max(rect.left + 10, 0), min(rect.right - 10, image.width), 2):
            if color_distance(image.getpixel((x, y)), target) <= 12:
                buckets.setdefault((x // 16, y // 16), []).append((x, y))
    if buckets:
        points = max(buckets.values(), key=len)
        anchor_x = round(sum(point[0] for point in points) / len(points))
        anchor_y = round(sum(point[1] for point in points) / len(points))
    else:
        anchor_x = rect.left + 46
        anchor_y = rect.top + 197
    CELL_X = tuple(anchor_x + 96 * column for column in range(4))
    CELL_Y = tuple(anchor_y + 97 * row for row in range(4))
    SUBMIT_XY = (anchor_x + 256, anchor_y - 62)


def capture_initial_board() -> tuple[int, ...]:
    board = capture_classes()
    unknown = [value for value in board if value not in TILE_COLORS]
    if unknown:
        raise RuntimeError(f"Unable to recognize initial board: {unknown}")
    return board


def find_window() -> int:
    matches: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if WINDOW_TITLE in title.value:
            matches.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        raise RuntimeError(f"Telegram mini app window {WINDOW_TITLE!r} was not found")
    return matches[0]


def focus_board(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    user32.SetCursorPos(CELL_X[1], CELL_Y[1])
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.25)


def press_direction(direction: str) -> None:
    key = KEYS[direction]
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, 0x0002, 0)


def click_submit() -> None:
    user32.SetCursorPos(*SUBMIT_XY)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def merge_line(line: tuple[int, ...]) -> tuple[tuple[int, ...], int]:
    compact = [value for value in line if value]
    result: list[int] = []
    gained = 0
    index = 0
    while index < len(compact):
        if index + 1 < len(compact) and compact[index] == compact[index + 1]:
            value = compact[index] * 2
            result.append(value)
            gained += value
            index += 2
        else:
            result.append(compact[index])
            index += 1
    result.extend([0] * (4 - len(result)))
    return tuple(result), gained


@lru_cache(maxsize=300_000)
def move(board: tuple[int, ...], direction: str) -> tuple[tuple[int, ...], int]:
    rows = [list(board[i : i + 4]) for i in range(0, 16, 4)]
    gained = 0
    if direction in ("left", "right"):
        for row_index in range(4):
            line = tuple(rows[row_index])
            if direction == "right":
                line = tuple(reversed(line))
            merged, points = merge_line(line)
            if direction == "right":
                merged = tuple(reversed(merged))
            rows[row_index] = list(merged)
            gained += points
    else:
        for column in range(4):
            line = tuple(rows[row][column] for row in range(4))
            if direction == "down":
                line = tuple(reversed(line))
            merged, points = merge_line(line)
            if direction == "down":
                merged = tuple(reversed(merged))
            for row in range(4):
                rows[row][column] = merged[row]
            gained += points
    return tuple(value for row in rows for value in row), gained


def log_board(board: tuple[int, ...]) -> tuple[float, ...]:
    return tuple(math.log2(value) if value else 0.0 for value in board)


@lru_cache(maxsize=300_000)
def evaluate(board: tuple[int, ...]) -> float:
    logs = log_board(board)
    empty = board.count(0)
    smoothness = 0.0
    for row in range(4):
        for column in range(4):
            index = row * 4 + column
            if not board[index]:
                continue
            if column < 3 and board[index + 1]:
                smoothness -= abs(logs[index] - logs[index + 1])
            if row < 3 and board[index + 4]:
                smoothness -= abs(logs[index] - logs[index + 4])

    monotonicity = 0.0
    for row in range(4):
        values = logs[row * 4 : row * 4 + 4]
        monotonicity += max(
            sum(values[i] - values[i + 1] for i in range(3) if values[i] > values[i + 1]),
            sum(values[i + 1] - values[i] for i in range(3) if values[i + 1] > values[i]),
        )
    for column in range(4):
        values = tuple(logs[row * 4 + column] for row in range(4))
        monotonicity += max(
            sum(values[i] - values[i + 1] for i in range(3) if values[i] > values[i + 1]),
            sum(values[i + 1] - values[i] for i in range(3) if values[i + 1] > values[i]),
        )

    maximum = max(board)
    max_index = board.index(maximum)
    corner_bonus = math.log2(maximum) if max_index in (0, 3, 12, 15) else 0.0
    merge_potential = sum(
        1
        for index, value in enumerate(board)
        if value
        and ((index % 4 < 3 and board[index + 1] == value) or (index < 12 and board[index + 4] == value))
    )
    return (
        empty * 300.0
        + monotonicity * 38.0
        + smoothness * 12.0
        + corner_bonus * 110.0
        + merge_potential * 75.0
    )


def search_depth(board: tuple[int, ...]) -> int:
    return 3


def best_move(board: tuple[int, ...]) -> tuple[str | None, float]:
    depth = search_depth(board)
    cache: dict[tuple[tuple[int, ...], int, bool], float] = {}

    def expectimax(state: tuple[int, ...], remaining: int, chance: bool) -> float:
        key = (state, remaining, chance)
        if key in cache:
            return cache[key]
        if remaining <= 0:
            return evaluate(state)
        if chance:
            empty_cells = [index for index, value in enumerate(state) if value == 0]
            if not empty_cells:
                return expectimax(state, remaining, False)
            total = 0.0
            for index in empty_cells:
                for value, probability in ((2, 0.9), (4, 0.1)):
                    spawned = list(state)
                    spawned[index] = value
                    total += probability * expectimax(tuple(spawned), remaining, False)
            result = total / len(empty_cells)
        else:
            candidates = []
            for direction in DIRECTIONS:
                moved, gained = move(state, direction)
                if moved != state:
                    candidates.append(expectimax(moved, remaining - 1, True) + gained * 0.25)
            result = max(candidates) if candidates else -1_000_000.0
        cache[key] = result
        return result

    choices: list[tuple[float, str]] = []
    for direction in DIRECTIONS:
        moved, gained = move(board, direction)
        if moved != board:
            choices.append((expectimax(moved, depth - 1, True) + gained * 0.25, direction))
    if not choices:
        return None, -1_000_000.0
    score, direction = max(choices)
    return direction, score


def detect_spawn(moved: tuple[int, ...], timeout: float = 1.2) -> tuple[int, int] | None:
    time.sleep(0.28)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        visible = capture_classes()
        candidates = [
            (index, visible[index])
            for index, value in enumerate(moved)
            if value == 0 and visible[index] in (2, 4)
        ]
        if len(candidates) == 1:
            return candidates[0]
        time.sleep(0.045)
    return None


def format_board(board: tuple[int, ...]) -> str:
    return "/".join(",".join(f"{value:4d}" for value in board[row : row + 4]) for row in range(0, 16, 4))


def simulate_once(initial: tuple[int, ...], target_score: int, seed: int) -> tuple[int, int]:
    rng = random.Random(seed)
    board = initial
    score = 0
    moves = 0
    while score < target_score:
        direction, _ = best_move(board)
        if direction is None:
            break
        board, gained = move(board, direction)
        score += gained
        empty = [index for index, value in enumerate(board) if not value]
        index = rng.choice(empty)
        spawned = list(board)
        spawned[index] = 4 if rng.random() < 0.1 else 2
        board = tuple(spawned)
        moves += 1
    return score, max(board)


def run_live(args: argparse.Namespace) -> int:
    hwnd = find_window()
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    calibrate_board(hwnd)
    focus_board(hwnd)
    board = capture_initial_board()
    score = args.start_score
    print(json.dumps({"event": "start", "score": score, "board": board}, ensure_ascii=False), flush=True)

    for turn in range(1, args.max_moves + 1):
        direction, utility = best_move(board)
        if direction is None:
            click_submit()
            print(json.dumps({"event": "game_over", "turn": turn, "score": score, "max": max(board)}), flush=True)
            return 2

        moved, gained = move(board, direction)
        press_direction(direction)
        spawn = detect_spawn(moved)
        if spawn is None:
            focus_board(hwnd)
            print(json.dumps({"event": "desync", "turn": turn, "direction": direction, "board": board}), flush=True)
            return 3

        spawn_index, spawn_value = spawn
        updated = list(moved)
        updated[spawn_index] = spawn_value
        board = tuple(updated)
        score += gained

        if turn % args.report_every == 0 or gained >= 512:
            print(
                json.dumps(
                    {
                        "event": "progress",
                        "turn": turn,
                        "score": score,
                        "max": max(board),
                        "empty": board.count(0),
                        "direction": direction,
                        "utility": round(utility, 1),
                        "board": format_board(board),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if score >= args.target_score:
            click_submit()
            time.sleep(1.0)
            print(json.dumps({"event": "target", "turn": turn, "score": score, "max": max(board)}), flush=True)
            return 0
    click_submit()
    print(json.dumps({"event": "move_limit", "score": score, "max": max(board)}), flush=True)
    return 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-score", type=int, default=120_000)
    parser.add_argument("--start-score", type=int, default=5_712)
    parser.add_argument("--max-moves", type=int, default=30_000)
    parser.add_argument("--report-every", type=int, default=100)
    parser.add_argument("--simulate", type=int, default=0, help="Run N offline games instead of controlling Telegram")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.simulate:
        initial = (256, 4, 2, 0, 128, 2, 4, 0, 32, 4, 2, 0, 0, 0, 0, 0)
        for seed in range(args.simulate):
            score, maximum = simulate_once(initial, args.target_score - args.start_score, seed)
            print(json.dumps({"seed": seed, "score": score + args.start_score, "max": maximum}))
        return 0
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
