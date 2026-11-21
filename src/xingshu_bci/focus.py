"""High-level helpers for consuming focus/relaxation analysis events.

The local API ships raw ``focus.prediction`` (0-1) at ~10 Hz embedded in
``analysis`` WebSocket events. For real-world feedback applications
(lightbulbs, motors, video stimuli, games) you typically also want:

* EMA smoothing — dampens noise on the raw prediction.
* Hysteresis thresholds — separate ON/OFF thresholds so the boolean
  state does not flicker around a single value.
* Rate limiting — cap the update frequency so downstream IoT devices
  are not flooded.
* Dead-zone & range mapping — convert the smoothed score to a useful
  control range (0-100 brightness, 0-255 PWM, etc.).

``SmoothedFocusStream`` bundles all of that as an iterator of
``FocusUpdate`` snapshots so application code can stay short.

Example
-------

>>> from xingshu_bci import XingShuClient, SmoothedFocusStream
>>> client = XingShuClient.auto()
>>> for upd in SmoothedFocusStream(client):
...     print(upd.smoothed, upd.is_on, upd.brightness)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator, Mapping, Optional

from .client import XingShuClient


@dataclass(frozen=True)
class FocusUpdate:
    """One throttled, smoothed focus reading suitable for actuator control."""

    raw: float                # raw model prediction (0-1) from this event
    smoothed: float           # EMA-smoothed value (0-1)
    is_on: bool               # current hysteresis boolean state
    brightness: int           # mapped output in [0, 100]
    timestamp: float          # time.monotonic() of the update
    rising: bool              # True only on the event that flipped OFF -> ON
    falling: bool             # True only on the event that flipped ON -> OFF


class SmoothedFocusStream:
    """Iterator wrapping ``client.events`` that yields ``FocusUpdate``.

    Parameters
    ----------
    client : XingShuClient
        A connected client. The stream assumes a device is acquiring data.
    metric : str
        ``"concentration"`` (default) or ``"relaxation"``. The stream will
        push this config to the server on entry (no-op if already set).
    smoothing : float
        EMA alpha in (0, 1]. Lower = smoother but laggier. Default 0.25.
    on_threshold, off_threshold : float
        Hysteresis bounds on the smoothed value. ``off_threshold`` must be
        strictly less than ``on_threshold``. Defaults: 0.45 / 0.30.
    dead_zone : float
        Smoothed values below this map to brightness 0. Default 0.20.
    min_interval_s : float
        Minimum seconds between yielded updates (rate limit). Default 0.2.
    auto_configure : bool
        When True (default), push ``focus.metric`` to the server before
        iterating so the right ML model is loaded.
    """

    def __init__(
        self,
        client: XingShuClient,
        *,
        metric: str = "concentration",
        smoothing: float = 0.25,
        on_threshold: float = 0.45,
        off_threshold: float = 0.30,
        dead_zone: float = 0.20,
        min_interval_s: float = 0.2,
        auto_configure: bool = True,
    ) -> None:
        if metric not in ("concentration", "relaxation"):
            raise ValueError("metric must be 'concentration' or 'relaxation'")
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be in (0, 1]")
        if not 0.0 <= off_threshold < on_threshold <= 1.0:
            raise ValueError("off_threshold < on_threshold required, both in [0, 1]")
        if not 0.0 <= dead_zone < 1.0:
            raise ValueError("dead_zone must be in [0, 1)")
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")

        self._client = client
        self._metric = metric
        self._alpha = smoothing
        self._on_thr = on_threshold
        self._off_thr = off_threshold
        self._dead = dead_zone
        self._min_dt = min_interval_s
        self._auto_configure = auto_configure

        # Internal state
        self._smoothed = 0.0
        self._is_on = False
        self._last_emit = 0.0

    # ── Public API ─────────────────────────────────────────────────────

    @property
    def smoothed(self) -> float:
        return self._smoothed

    @property
    def is_on(self) -> bool:
        return self._is_on

    def map_brightness(self, smoothed: float) -> int:
        """Linear map of smoothed value to integer brightness 0-100."""
        if smoothed <= self._dead:
            return 0
        pct = (smoothed - self._dead) / (1.0 - self._dead) * 100.0
        return int(round(max(0.0, min(100.0, pct))))

    def __iter__(self) -> Iterator[FocusUpdate]:
        if self._auto_configure:
            try:
                cfg = self._client.get_analysis_config()
                cur = (cfg.get("focus") or {}).get("metric")
                if cur != self._metric:
                    # The platform treats this endpoint as a full config
                    # replacement. Preserve FFT/band/head/source settings
                    # instead of resetting them while selecting the metric.
                    next_config = dict(cfg)
                    next_config["focus"] = {
                        **(cfg.get("focus") or {}),
                        "metric": self._metric,
                    }
                    self._client.set_analysis_config(**next_config)
            except Exception:
                # Non-fatal: server may not allow config changes, or may be
                # already in the desired state. Iteration proceeds either way.
                pass

        last_focus_revision = None
        last_epoch = None
        for event in self._client.events(types=["analysis"]):
            # Other widgets can publish snapshots while focus is unchanged.
            # Apply EMA once per computed focus result, independently of the
            # FFT/head-plot cadence. Epochs prevent reconnect/reset collisions.
            runtime = event.payload.get("runtime")
            revisions = runtime.get("revisions") if isinstance(runtime, Mapping) else None
            revision = None
            if isinstance(revisions, Mapping) and revisions.get("focus") is not None:
                revision = (
                    runtime.get("sessionEpoch"), runtime.get("analysisEpoch"),
                    revisions["focus"],
                )
                if revision == last_focus_revision:
                    continue
                epoch = revision[:2]
                if last_epoch is not None and epoch != last_epoch:
                    self._smoothed = 0.0
                    self._is_on = False
                    self._last_emit = 0.0
                last_epoch = epoch
            focus = (event.payload or {}).get("focus") or {}
            if not isinstance(focus, Mapping) or focus.get("state") != "ready":
                continue
            last_focus_revision = revision

            try:
                raw = float(focus.get("prediction") or 0.0)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= raw <= 1.0:
                continue
            self._smoothed = self._alpha * raw + (1.0 - self._alpha) * self._smoothed

            now = time.monotonic()
            if now - self._last_emit < self._min_dt:
                continue
            self._last_emit = now

            rising = falling = False
            if not self._is_on and self._smoothed >= self._on_thr:
                self._is_on = True
                rising = True
            elif self._is_on and self._smoothed <= self._off_thr:
                self._is_on = False
                falling = True

            yield FocusUpdate(
                raw=raw,
                smoothed=self._smoothed,
                is_on=self._is_on,
                brightness=self.map_brightness(self._smoothed),
                timestamp=now,
                rising=rising,
                falling=falling,
            )


# Convenience function ─────────────────────────────────────────────────

def smoothed_focus(
    client: XingShuClient,
    **kwargs,
) -> SmoothedFocusStream:
    """Shorthand: ``for upd in smoothed_focus(client): ...``"""
    return SmoothedFocusStream(client, **kwargs)
