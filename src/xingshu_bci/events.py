"""WebSocket event helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


@dataclass
class Event:
    """A single WebSocket event envelope from the local API."""

    type: str
    seq: int = 0
    timestampMs: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        raw_payload = data.get("payload")
        # A malformed or non-object payload must never leak through as a
        # list/str/None: every consumer treats payload as a mapping.
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        return cls(
            type=str(data.get("type", "") or ""),
            seq=_safe_int(data.get("seq", 0) or 0),
            timestampMs=_safe_int(data.get("timestampMs", 0) or 0),
            payload=payload,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"Event(type={self.type!r}, seq={self.seq}, payload_keys={list(self.payload.keys())[:4]})"
