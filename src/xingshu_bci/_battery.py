"""Battery state-of-charge estimator, ported from the platform's battery-estimator.js.

Kept behaviour-identical so a script talking to the device directly reports the
same percentage and state as the desktop platform:

* the pack voltage is low-pass filtered with spike rejection;
* while charging, the terminal-voltage rise is compensated and the level never
  drops; in the constant-voltage phase it creeps toward 99 %;
* "full" comes only from the charger IC (adapter present and CHGOK# released),
  never from the voltage.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

LI_ION_CELL_OCV_CURVE = (
    (3300, 0), (3550, 3), (3610, 5), (3690, 10), (3710, 15), (3730, 20), (3750, 25),
    (3770, 30), (3790, 35), (3800, 40), (3820, 45), (3840, 50), (3850, 55), (3870, 60),
    (3910, 65), (3950, 70), (3980, 75), (4020, 80), (4080, 85), (4110, 90), (4150, 95),
    (4190, 100),
)

DEFAULTS: Dict[str, float] = {
    "filterTauMs": 20000,
    "settleTauMs": 3000,
    "settleMs": 8000,
    "chargeOffsetCellMv": 70,
    "cvCellMv": 4150,
    "cvCreepMs": 60000,
    "adapterSettleMs": 3000,
    "spikeCellMv": 250,
    "spikeConfirmSamples": 3,
    "stepIntervalMs": 4000,
    "jumpPercent": 25,
    "lowPercent": 20,
    "criticalPercent": 8,
}


def cell_percent(cell_mv: float) -> Optional[float]:
    if cell_mv is None or not math.isfinite(cell_mv):
        return None
    curve = LI_ION_CELL_OCV_CURVE
    if cell_mv <= curve[0][0]:
        return 0.0
    for i in range(1, len(curve)):
        v1, p1 = curve[i]
        if cell_mv <= v1:
            v0, p0 = curve[i - 1]
            return p0 + ((cell_mv - v0) / (v1 - v0)) * (p1 - p0)
    return 100.0


def _opt_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


class BatteryEstimator:
    def __init__(self, cell_count: int = 1, **options: float) -> None:
        self.cell_count = max(1, int(round(cell_count or 1)))
        self.o = {**DEFAULTS, **options}
        self.reset()

    def reset(self) -> None:
        self.filtered_mv: Optional[float] = None
        self.last_ms: Optional[float] = None
        self.displayed: Optional[float] = None
        self.last_step_ms: Optional[float] = None
        self.mode: Optional[str] = None
        self.mode_since_ms: float = 0.0
        self.spike_count = 0
        self.cv_creep = 0
        self.cv_base: Optional[float] = None
        self.last_creep_ms: Optional[float] = None
        self.samples = 0
        self.resync = False

    def update(
        self,
        voltage_mv: Optional[float],
        *,
        valid: Optional[bool] = None,
        adapter_present: Optional[bool] = None,
        charging: Optional[bool] = None,
        charge_complete: Optional[bool] = None,
        now_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        o = self.o
        now = float(now_ms) if now_ms is not None else time.time() * 1000.0
        raw = float(voltage_mv) if isinstance(voltage_mv, (int, float)) else float("nan")
        adapter = _opt_bool(adapter_present)
        chg = _opt_bool(charging)
        complete = _opt_bool(charge_complete)
        base = {
            "cellCount": self.cell_count,
            "rawVoltageMv": raw if math.isfinite(raw) else None,
            "adapterPresent": adapter,
            "charging": chg is True,
            "chargeComplete": complete is True,
        }
        if valid is False or not math.isfinite(raw) or raw <= 0:
            self.reset()
            return {**base, "valid": False, "state": "unknown", "percent": None, "voltageMv": None,
                    "cellMv": None, "low": False, "critical": False, "estimating": False}

        if chg is True:
            mode = "charging"
        elif complete is True:
            mode = "full"
        elif adapter is True:
            mode = "adapter"
        else:
            mode = "discharging"

        if mode != self.mode:
            if self.mode is not None:
                self.resync = True
            self.mode = mode
            self.mode_since_ms = now
            self.spike_count = 0
            self.cv_creep = 0
            self.cv_base = None
            self.last_creep_ms = None

        if self.filtered_mv is None:
            self.filtered_mv = raw
        else:
            deviation_cell = abs(raw - self.filtered_mv) / self.cell_count
            settling = now - self.mode_since_ms < o["settleMs"]
            if not settling and deviation_cell > o["spikeCellMv"]:
                self.spike_count += 1
                if self.spike_count < o["spikeConfirmSamples"]:
                    return self._result(base, now, mode, spike=True)
            self.spike_count = 0
            last = self.last_ms if self.last_ms is not None else now
            dt = max(0.0, min(60000.0, now - last))
            tau = o["settleTauMs"] if settling else o["filterTauMs"]
            alpha = 1.0 if deviation_cell > o["spikeCellMv"] else 1.0 - math.exp(-dt / tau)
            self.filtered_mv += alpha * (raw - self.filtered_mv)
        self.last_ms = now
        self.samples += 1
        return self._result(base, now, mode, spike=False)

    def _result(self, base: Dict[str, Any], now: float, mode: str, *, spike: bool) -> Dict[str, Any]:
        o = self.o
        assert self.filtered_mv is not None
        cell_mv = self.filtered_mv / self.cell_count
        state = mode
        if mode == "charging":
            target = min(99.0, cell_percent(cell_mv - o["chargeOffsetCellMv"]) or 0.0)
            if cell_mv >= o["cvCellMv"]:
                if self.last_creep_ms is None:
                    self.last_creep_ms = now
                steps = int((now - self.last_creep_ms) // o["cvCreepMs"])
                if steps > 0:
                    self.cv_creep += steps
                    self.last_creep_ms += steps * o["cvCreepMs"]
                if self.cv_base is None:
                    self.cv_base = target if self.resync else (self.displayed if self.displayed is not None else target)
                target = min(99.0, max(target, self.cv_base + self.cv_creep))
            else:
                self.last_creep_ms = None
                self.cv_base = None
                self.cv_creep = 0
        elif mode == "full":
            target = 100.0
        elif mode == "adapter":
            if now - self.mode_since_ms < o["adapterSettleMs"]:
                state = "plugged"
                target = cell_percent(cell_mv) or 0.0
            else:
                state = "full"
                target = 100.0
        else:
            state = "discharging"
            target = cell_percent(cell_mv) or 0.0

        if state == "charging":
            monotonic = "up"
        elif state == "discharging" and base["adapterPresent"] is False:
            monotonic = "down"
        else:
            monotonic = None
        if not spike:
            self._step(target, state, monotonic, now)
        percent = None if self.displayed is None else int(round(self.displayed))
        return {
            **base,
            "valid": True,
            "state": state,
            "percent": percent,
            "voltageMv": int(round(self.filtered_mv)),
            "cellMv": int(round(cell_mv)),
            "low": percent is not None and percent <= o["lowPercent"] and state not in ("charging", "full"),
            "critical": percent is not None and percent <= o["criticalPercent"] and state not in ("charging", "full"),
            "estimating": self.samples < 3,
        }

    def _step(self, target: float, state: str, monotonic: Optional[str], now: float) -> None:
        o = self.o
        if state == "full":
            self.displayed = 100.0
            self.last_step_ms = now
            return
        if self.resync and self.displayed is not None:
            if now - self.mode_since_ms < o["settleMs"]:
                return
            self.resync = False
            self.displayed = target
            self.last_step_ms = now
            return
        if self.displayed is None or abs(target - self.displayed) >= o["jumpPercent"]:
            self.displayed = target
            self.last_step_ms = now
            return
        goal = target
        if monotonic == "up":
            goal = max(goal, self.displayed)
        if monotonic == "down":
            goal = min(goal, self.displayed)
        last = self.last_step_ms if self.last_step_ms is not None else now
        max_step = max(0.0, (now - last) / o["stepIntervalMs"])
        if max_step < 1 and abs(goal - self.displayed) >= 1:
            return
        delta = max(-max_step, min(max_step, goal - self.displayed))
        self.displayed += delta
        self.last_step_ms = now
