"""Resolve a large battle-clock offset without guessing unsigned hit directions."""

from __future__ import annotations

import math
import statistics
from typing import Any


class BattleClockProbe:
    """Compare two ordinary strikes with two strikes released 80 ms later.

    Only stable, fast replies outside the perfect band can arm the probe. Both
    shifted strikes must support the same clock-offset hypothesis. It reserves
    existing windows, sends no requests, and attempts calibration once per round.
    """

    DELAY_MS = 80
    JITTER_MS = 35
    MAX_RTT_MS = 350
    MAX_OFFSET_MS = 400

    def __init__(self) -> None:
        self.baseline: list[dict[str, float]] = []
        self.reserved: set[str] = set()
        self.observed: set[str] = set()
        self.confirmations: list[tuple[str, dict[str, float]]] = []
        self.status = "idle"
        self.offset_ms: float | None = None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def plan(self, window_id: str, hit_ms: int) -> int:
        if window_id in self.reserved:
            return self.DELAY_MS
        if self.status not in {"armed", "probing"} or len(self.reserved) >= 2:
            return 0
        # Even the unfavorable sign must retain a margin inside the hit band.
        if max(row["delta"] for row in self.baseline) + self.DELAY_MS + 80 >= hit_ms:
            return 0
        self.reserved.add(window_id)
        self.status = "probing"
        return self.DELAY_MS

    def observe(
        self, window_id: str, *, delta_ms: Any, sent_offset_ms: Any,
        rtt_ms: Any, wake_lateness_ms: Any, perfect_ms: int, hit_ms: int,
        strict_direction: bool,
    ) -> dict[str, Any] | None:
        if self.status in {"confirmed", "rejected"} or window_id in self.observed:
            return None
        self.observed.add(window_id)
        delta, sent, rtt, wake = map(
            self._number, (delta_ms, sent_offset_ms, rtt_ms, wake_lateness_ms),
        )
        valid = (
            all(value is not None for value in (delta, sent, rtt, wake))
            and 0 < delta <= hit_ms and 0 < rtt <= self.MAX_RTT_MS
            and 0 <= wake <= 40
        )
        shifted = window_id in self.reserved
        if not valid:
            if shifted:
                self.status = "rejected"
                return {"status": self.status, "reason": "unstable_reply"}
            if not self.reserved:
                self.baseline.clear()
                self.status = "idle"
            return None
        row = {"delta": delta, "sent": sent, "rtt": rtt,
               "early": -delta - sent, "late": delta - sent}
        if shifted:
            base_sent = statistics.median(item["sent"] for item in self.baseline)
            base_rtt = statistics.median(item["rtt"] for item in self.baseline)
            shift = sent - base_sent
            matches = [
                direction for direction in ("early", "late")
                if abs(row[direction] - statistics.median(
                    item[direction] for item in self.baseline
                )) <= self.JITTER_MS
            ]
            if (not 50 <= shift <= 120 or abs(rtt - base_rtt) > 50
                    or len(matches) != 1
                    or (self.confirmations and self.confirmations[0][0] != matches[0])):
                self.status = "rejected"
                return {"status": self.status, "reason": "inconsistent_shift",
                        "actual_shift_ms": round(shift, 1)}
            direction = matches[0]
            self.confirmations.append((direction, row))
            diagnostic = {"status": "confirming", "direction": direction,
                          "actual_shift_ms": round(shift, 1),
                          "confirmation_count": len(self.confirmations)}
            if len(self.confirmations) == 2:
                values = [item[direction] for item in self.baseline]
                values.extend(item[direction] for _, item in self.confirmations)
                offset = statistics.median(values)
                if abs(offset) > self.MAX_OFFSET_MS or max(values) - min(values) > 45:
                    self.status = "rejected"
                    return {"status": self.status, "reason": "offset_out_of_bounds"}
                self.offset_ms = offset
                self.status = "confirmed"
                diagnostic.update(status=self.status, offset_ms=round(offset, 3))
            return diagnostic
        if self.reserved:
            return None  # An older in-flight strike cannot replace the baseline.
        if strict_direction or delta <= perfect_ms or delta + self.DELAY_MS + 80 >= hit_ms:
            self.baseline.clear()
            self.status = "idle"
            return None
        if self.baseline and any(
            abs(row[key] - self.baseline[-1][key]) > limit
            for key, limit in (("early", 35), ("late", 35), ("sent", 25), ("rtt", 40))
        ):
            self.baseline.clear()
        self.baseline.append(row)
        self.baseline = self.baseline[-2:]
        self.status = "armed" if len(self.baseline) == 2 else "collecting"
        return {"status": self.status, "baseline_count": len(self.baseline)}

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "shift_ms": self.DELAY_MS,
                "shifted_window_count": len(self.reserved),
                "confirmation_count": len(self.confirmations),
                "offset_ms": None if self.offset_ms is None else round(self.offset_ms, 3)}
