"""Choose normal movement/buttons from the official Nangongque room snapshots.

Coordinates, button cooldown floors and mechanic zones follow the published
Mini App client. Damage, role bonuses, phase changes and rewards remain entirely
server authoritative; the local preview's combat simulation is not used.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


BUTTON_COOLDOWNS = {"attack": 0.42, "dodge": 2.25, "mechanic": 1.55}
COOLDOWN_FIELDS = {"attack": "attackCdMs", "dodge": "dodgeCdMs", "mechanic": "mechCdMs"}
SWORD_ANCHORS = ((22.0, 40.0), (78.0, 42.0), (50.0, 62.0))
TERMINAL_ROOMS = frozenset({"cleared", "failed"})


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def self_player(snapshot: dict, player_id: Any) -> dict | None:
    """Never control the first other player when our row is missing."""
    players = [row for row in snapshot.get("players", []) if isinstance(row, dict)]
    matches = [row for row in players if str(row.get("id", "")) == str(player_id)]
    if len(matches) == 1:
        return matches[0]
    own = [row for row in players if row.get("self") is True]
    return own[0] if len(own) == 1 else None


def point_line_distance(x: float, y: float, hazard: dict) -> float:
    x1, y1, x2, y2 = (number(hazard.get(key)) for key in ("x1", "y1", "x2", "y2"))
    dx, dy = x2 - x1, y2 - y1
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(x - x1 - dx * t, y - y1 - dy * t)


def hazard_clearance(hazard: dict, x: float, y: float, radius: float = 2.2) -> float:
    """Positive values are outside the visible hazard with a body-sized margin."""
    margin = radius + 0.8
    kind = hazard.get("type")
    if kind in {"line", "blood"}:
        return point_line_distance(x, y, hazard) - number(hazard.get("width")) - margin
    if kind == "cone":
        dx, dy = x - number(hazard.get("x")), y - number(hazard.get("y"))
        distance = math.hypot(dx, dy)
        delta = math.atan2(math.sin(math.atan2(dy, dx) - number(hazard.get("angle"))),
                           math.cos(math.atan2(dy, dx) - number(hazard.get("angle"))))
        edge = distance * math.sin(min(math.pi / 2, abs(delta) - number(hazard.get("spread"))))
        return max(distance - number(hazard.get("range")) - margin, edge - margin)
    return math.inf


@dataclass(frozen=True)
class RoomInput:
    move_x: float = 0.0
    move_y: float = 0.0
    action: str = ""
    reason: str = "waiting"
    send: bool = True


class NangongqueStrategy:
    def __init__(self):
        self.ready_at = {action: 0.0 for action in BUTTON_COOLDOWNS}

    def sent(self, intent: RoomInput, now: float) -> None:
        # A lost response is still an attempted button press. Do not replay it.
        if intent.action in BUTTON_COOLDOWNS:
            self.ready_at[intent.action] = now + BUTTON_COOLDOWNS[intent.action]

    def _ready(self, action: str, player: dict, now: float) -> bool:
        return now >= self.ready_at[action] and number(player.get(COOLDOWN_FIELDS[action])) <= 0

    @staticmethod
    def _objective(snapshot: dict, player: dict) -> tuple[tuple[float, float], bool, str]:
        x, y = number(player.get("x"), 50), number(player.get("y"), 78)
        boss, mechanics = snapshot.get("boss") or {}, snapshot.get("mechanics") or {}
        phase = int(number(boss.get("phase"), 1))
        if phase == 1 and number(mechanics.get("swordThreads")) > 0:
            goal = min(SWORD_ANCHORS, key=lambda p: math.hypot(x - p[0], y - p[1]))
            return goal, math.hypot(x - goal[0], y - goal[1]) <= 11, "sword_threads"
        if phase == 2 and number(mechanics.get("mirrorLock")) > 0:
            bx, by = number(boss.get("x"), 50), number(boss.get("y"), 24)
            return (bx, by + 23), math.hypot(x - bx, y - by - 10) <= 23, "mirror"
        if phase == 3 and number(mechanics.get("bloodBane")) >= 12:
            return (50, 66), y > 53 and 31 < x < 69, "blood_bane"
        if phase == 4:
            if number(mechanics.get("wanGuard"), 100) < 80:
                return (50, 84), math.hypot(x - 50, y - 84) <= 11, "guard_wan"
            if number(mechanics.get("bloodBane")) >= 45:
                return (50, 66), y > 53 and 31 < x < 69 and math.hypot(x - 50, y - 84) > 15, "blood_bane"
            if number(mechanics.get("ambush")) < 100:
                goal = min(((50, 30), (50, 84)), key=lambda p: math.hypot(x - p[0], y - p[1]))
                return goal, math.hypot(x - goal[0], y - goal[1]) <= 11, "final_ambush"
        return (50, 62 if phase < 4 else 76), False, "attack"

    def decide(self, snapshot: dict, player_id: Any, now: float, *, snapshot_age: float = 0.0) -> RoomInput:
        status = str((snapshot.get("room") or {}).get("status") or "")
        if status in TERMINAL_ROOMS:
            return RoomInput(reason="room_finished", send=False)
        if not status:
            return RoomInput(reason="missing_room", send=False)
        if snapshot_age > 1.5:
            return RoomInput(reason="stale_state", send=False)
        if status == "joining":
            return RoomInput(reason="joining")
        player = self_player(snapshot, player_id)
        if player is None:
            return RoomInput(reason="missing_self", send=False)
        if number(player.get("hp")) <= 0:
            return RoomInput(reason="incapacitated", send=False)
        if number((snapshot.get("boss") or {}).get("hp")) <= 0:
            return RoomInput(reason="await_settlement", send=False)
        x, y = number(player.get("x"), 50), number(player.get("y"), 78)
        radius = max(1.0, number(player.get("r"), 2.2))
        goal, can_mechanic, objective = self._objective(snapshot, player)
        distance = math.hypot(goal[0] - x, goal[1] - y)
        desired = ((goal[0] - x) / max(6.0, distance), (goal[1] - y) / max(6.0, distance)) if distance > 2 else (0.0, 0.0)
        hazards = [h for h in snapshot.get("hazards", []) if isinstance(h, dict)
                   and not h.get("hit") and number(h.get("life")) > snapshot_age
                   and number(h.get("warn")) - snapshot_age <= 1.0]

        def position(vector, seconds):
            return (max(7.0, min(93.0, x + vector[0] * 24 * seconds)),
                    max(16.0, min(92.0, y + vector[1] * 24 * seconds)))

        def score(vector):
            end = position(vector, 0.48)
            value = -math.hypot(end[0] - goal[0], end[1] - goal[1])
            # Test the position at each hazard's actual warning deadline, not
            # just the endpoint (which could cross the attack before escaping).
            for hazard in hazards:
                until = max(0.0, number(hazard.get("warn")) - snapshot_age)
                point = position(vector, max(0.05, min(0.65, until)))
                clearance = hazard_clearance(hazard, *point, radius)
                value += min(0.0, clearance) * 250 - (150 if clearance <= 0 else 0)
            return value - 0.2 * math.hypot(vector[0], vector[1])

        candidates = [desired, (0.0, 0.0)]
        if hazards:
            candidates.extend((math.cos(i * math.pi / 8), math.sin(i * math.pi / 8)) for i in range(16))
        move = max(candidates, key=score)
        imminent = [h for h in hazards if number(h.get("warn")) - snapshot_age <= 0.38
                    and hazard_clearance(h, x, y, radius) <= 0]
        if imminent and number(player.get("invulnMs")) <= 0 and self._ready("dodge", player, now):
            if math.hypot(*move) < 0.1:
                # Choose a facing direction even if walking cannot clear the
                # hit in time. The server executes the ordinary dash itself.
                move = max(candidates[2:] or [(0.0, 1.0)],
                           key=lambda v: min(hazard_clearance(h, *position(v, 0.3), radius) for h in imminent))
            return RoomInput(*move, "dodge", "avoid_hazard")
        # Mechanic buttons use our confirmed position and the correct phase;
        # movement is never fabricated as a client-side position update.
        if can_mechanic and self._ready("mechanic", player, now):
            return RoomInput(*move, "mechanic", objective)
        if self._ready("attack", player, now):
            return RoomInput(*move, "attack", objective)
        return RoomInput(*move, reason=objective)
