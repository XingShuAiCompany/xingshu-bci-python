"""Lightweight dataclass models mirroring the API schema."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


_LEGACY_BOARD_COUNTS = frozenset({1, 2})
_PRO_BOARD_COUNTS = frozenset({4})


def _normalise_board_kind(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    if text in {"xingshun1pro", "xingshu_n1_pro", "n1_pro", "n1pro"}:
        return "xingshu_n1_pro"
    if text in {"xingshu_n1", "xingshun1", "n1"}:
        return "xingshu_n1"
    return text


def _valid_board_count(board_kind: Optional[str], count: Optional[int]) -> bool:
    if count is None:
        return False
    allowed = _PRO_BOARD_COUNTS if board_kind == "xingshu_n1_pro" else _LEGACY_BOARD_COUNTS
    return count in allowed


@dataclass
class VersionInfo:
    appName: str = ""
    appVersion: str = ""
    apiVersion: str = ""
    protocol: str = ""
    brainflowVersion: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VersionInfo":
        return cls(**{k: data.get(k, "") for k in cls.__dataclass_fields__.keys()})


@dataclass
class XingShuTopology:
    state: str = "unknown"
    source: Optional[str] = None
    configuredBoardCount: Optional[int] = None
    verifiedBoardCount: Optional[int] = None
    detectedBoardCount: Optional[int] = None
    boardCount: Optional[int] = None
    channelCount: Optional[int] = None
    detectionRaw: Optional[int] = None
    detectionValid: bool = False
    adsVerified: bool = False
    locked: bool = False
    stale: bool = False
    epoch: Optional[int] = None
    recoveryAttempts: int = 0
    closeReason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    boardKind: Optional[str] = None

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        board_kind: Optional[str] = None,
    ) -> "XingShuTopology":
        board_kind = _normalise_board_kind(
            board_kind
            or data.get("board")
            or data.get("boardKind")
            or data.get("model")
        )
        state = str(data.get("state") or "unknown")
        ready_state = state in {"ready", "ready-auto", "ready-manual"}
        source_value = data.get("source")
        if source_value is None and state == "ready-auto":
            source_value = "auto"
        elif source_value is None and state == "ready-manual":
            source_value = "manual"
        source = str(source_value) if source_value in {"auto", "manual", "fixed"} else None
        board_value = data.get("boardCount")
        channel_value = data.get("channelCount")
        configured_value = data.get(
            "configuredBoardCount",
            data.get("detectedBoardCount", data.get("detectedBoards")),
        )
        verified_value = data.get("verifiedBoardCount")
        detection_valid = bool(data.get("detectionValid", False))
        ads_verified = bool(data.get("adsVerified", False))
        locked = bool(data.get("locked", False))
        stale = bool(data.get("stale", False))
        board_count = int(board_value) if board_value is not None else None
        channel_count = int(channel_value) if channel_value is not None else None
        configured_count = (
            int(configured_value) if configured_value is not None else board_count
        )
        verified_count = (
            int(verified_value)
            if verified_value is not None
            else (board_count if ads_verified else None)
        )
        topology_source_valid = (
            (source == "manual" and _valid_board_count(board_kind, configured_count))
            or (source == "auto" and detection_valid)
            or (source == "fixed" and board_count in _PRO_BOARD_COUNTS
                and configured_count == board_count)
        )
        usable = (
            ready_state
            and _valid_board_count(board_kind, board_count)
            and configured_count == board_count
            and verified_count == board_count
            and channel_count == board_count * 8
            and topology_source_valid
            and ads_verified
            and locked
            and not stale
        )
        return cls(
            state="ready" if usable else state,
            source=source,
            configuredBoardCount=configured_count,
            verifiedBoardCount=verified_count,
            detectedBoardCount=configured_count,
            boardCount=board_count if usable else None,
            channelCount=channel_count if usable else None,
            detectionRaw=data.get("detectionRaw"),
            detectionValid=detection_valid,
            adsVerified=ads_verified,
            locked=locked,
            stale=stale,
            epoch=data.get("epoch"),
            recoveryAttempts=max(0, min(3, int(data.get("recoveryAttempts", 0) or 0))),
            closeReason=(
                str(data["closeReason"])
                if data.get("closeReason") is not None
                else None
            ),
            boardKind=board_kind,
            raw=dict(data),
        )

    @property
    def detectedBoards(self) -> Optional[int]:
        return self.configuredBoardCount

    @property
    def ready(self) -> bool:
        return (
            self.state == "ready"
            and self.source in {"auto", "manual", "fixed"}
            and _valid_board_count(self.boardKind, self.boardCount)
            and self.configuredBoardCount == self.boardCount
            and self.verifiedBoardCount == self.boardCount
            and self.channelCount == self.boardCount * 8
            and (
                (self.source == "manual")
                or (self.source == "auto" and self.detectionValid)
                or (self.source == "fixed" and self.boardCount in _PRO_BOARD_COUNTS)
            )
            and self.adsVerified
            and self.locked
            and not self.stale
        )


@dataclass
class XingShuCapabilities:
    topology: XingShuTopology = field(default_factory=XingShuTopology)
    boardCount: Optional[int] = None
    channelCount: Optional[int] = None
    sampleRates: List[int] = field(default_factory=list)
    transports: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    board: Optional[str] = None

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        board: Optional[str] = None,
    ) -> "XingShuCapabilities":
        topology_data = data.get("topology")
        topology_hints = topology_data if isinstance(topology_data, Mapping) else {}
        board_kind = _normalise_board_kind(
            board
            or data.get("board")
            or data.get("boardKind")
            or data.get("model")
            or topology_hints.get("board")
            or topology_hints.get("boardKind")
            or topology_hints.get("model")
        )
        topology = (
            XingShuTopology.from_dict(topology_data, board_kind=board_kind)
            if isinstance(topology_data, Mapping)
            else XingShuTopology(boardKind=board_kind)
        )
        return cls(
            topology=topology,
            board=board_kind,
            boardCount=topology.boardCount,
            channelCount=topology.channelCount,
            sampleRates=[int(rate) for rate in (
                data.get("sampleRates") or data.get("supportedSampleRates")
                if isinstance(data.get("sampleRates") or data.get("supportedSampleRates"), list)
                else [data.get("sampleRates") or data.get("supportedSampleRates")]
            ) if rate is not None],
            transports=[str(transport) for transport in (
                data.get("transports") if isinstance(data.get("transports"), list)
                else [data.get("transports")] if data.get("transports") is not None else []
            )],
            raw=dict(data),
        )


@dataclass
class BatteryStatus:
    """Device battery as filtered by the platform (``telemetry.battery``).

    ``state`` is one of ``discharging``, ``charging``, ``full``, ``plugged`` or
    ``unknown``. Whether the pack is full comes from the charger IC status pin
    (MP2615 CHGOK# on the N1 Pro), never from the voltage.
    """

    valid: bool = False
    percent: Optional[int] = None
    state: str = "unknown"
    voltageMv: Optional[int] = None
    cellMv: Optional[int] = None
    cellCount: Optional[int] = None
    charging: bool = False
    chargeComplete: bool = False
    adapterPresent: Optional[bool] = None
    low: bool = False
    critical: bool = False
    receivedAtMs: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BatteryStatus":
        percent = data.get("percent")
        return cls(
            valid=bool(data.get("valid", percent is not None)),
            percent=int(round(percent)) if isinstance(percent, (int, float)) and math.isfinite(percent) else None,
            state=str(data.get("state") or ("charging" if data.get("charging") else "unknown")),
            voltageMv=_optional_int(data.get("voltageMv")),
            cellMv=_optional_int(data.get("cellMv")),
            cellCount=_optional_int(data.get("cellCount")),
            charging=bool(data.get("charging", False)),
            chargeComplete=bool(data.get("chargeComplete", False)),
            adapterPresent=data.get("adapterPresent") if isinstance(data.get("adapterPresent"), bool) else None,
            low=bool(data.get("low", False)),
            critical=bool(data.get("critical", False)),
            receivedAtMs=data.get("receivedAtMs") if isinstance(data.get("receivedAtMs"), (int, float)) else None,
            raw=dict(data),
        )


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(round(value))


@dataclass
class ServerStatus:
    state: str = "idle"
    board: Optional[str] = None
    transport: Optional[str] = None
    sampleRate: Optional[float] = None
    channelCount: Optional[int] = None
    capabilities: Optional[XingShuCapabilities] = None
    filterConfig: Dict[str, Any] = field(default_factory=dict)
    analysisConfig: Dict[str, Any] = field(default_factory=dict)
    brainflowVersion: Optional[str] = None
    lastError: Optional[str] = None
    telemetry: Dict[str, Any] = field(default_factory=dict)
    battery: Optional[BatteryStatus] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerStatus":
        telemetry = data.get("telemetry") if isinstance(data.get("telemetry"), Mapping) else {}
        battery = telemetry.get("battery")
        return cls(
            state=data.get("state", "idle"),
            board=data.get("board"),
            transport=data.get("transport"),
            sampleRate=data.get("sampleRate"),
            channelCount=data.get("channelCount"),
            capabilities=XingShuCapabilities.from_dict(
                data["capabilities"],
                board=data.get("board"),
            )
            if isinstance(data.get("capabilities"), Mapping)
            else None,
            filterConfig=data.get("filterConfig", {}) or {},
            analysisConfig=data.get("analysisConfig", {}) or {},
            brainflowVersion=data.get("brainflowVersion"),
            lastError=data.get("lastError"),
            telemetry=dict(telemetry),
            battery=BatteryStatus.from_dict(battery) if isinstance(battery, Mapping) else None,
            raw=data,
        )


@dataclass
class TokenInfo:
    id: str
    name: str
    scopes: List[str]
    createdAt: str
    expiresAt: Optional[str] = None
    lastUsedAt: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenInfo":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            scopes=list(data.get("scopes", []) or []),
            createdAt=data.get("createdAt", ""),
            expiresAt=data.get("expiresAt"),
            lastUsedAt=data.get("lastUsedAt"),
        )


@dataclass
class TokenCreated(TokenInfo):
    token: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TokenCreated":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            scopes=list(data.get("scopes", []) or []),
            createdAt=data.get("createdAt", ""),
            expiresAt=data.get("expiresAt"),
            lastUsedAt=data.get("lastUsedAt"),
            token=data.get("token", ""),
        )


@dataclass
class ExperimentModelRegistration:
    """An external model registered with the local experiment runtime."""

    model_id: str
    name: str
    version: Optional[str] = None
    paradigm: str = "ssvep"
    registered_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    status: str = "online"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExperimentModelRegistration":
        model_id = str(data.get("modelId") or data.get("id") or "").strip()
        if not model_id:
            raise ValueError("Experiment model registration is missing modelId")
        return cls(
            model_id=model_id,
            name=str(data.get("name") or ""),
            version=str(data["version"]) if data.get("version") is not None else None,
            paradigm=str(data.get("paradigm") or "ssvep"),
            registered_at=data.get("registeredAt"),
            last_seen_at=data.get("lastSeenAt"),
            status=str(data.get("status") or "online"),
            raw=dict(data),
        )

    @property
    def modelId(self) -> str:
        return self.model_id

    @property
    def registeredAt(self) -> Optional[str]:
        return self.registered_at

    @property
    def lastSeenAt(self) -> Optional[str]:
        return self.last_seen_at


@dataclass
class SsvepTrialWindow:
    """A channel-major EEG window delivered to an external SSVEP model."""

    model_id: str
    run_id: str
    trial_id: str
    attempt_id: str
    experiment_id: str
    sample_rate: float
    channel_labels: List[str]
    data: List[List[float]]
    targets: List[Dict[str, Any]]
    attempt: Optional[int] = None
    sample_index_start: Optional[int] = None
    sample_index_end: Optional[int] = None
    acquisition_sample_start: Optional[int] = None
    acquisition_sample_end_exclusive: Optional[int] = None
    recording_sample_start: Optional[int] = None
    recording_sample_end_exclusive: Optional[int] = None
    window_start_timestamp_ms: Optional[float] = None
    window_end_timestamp_ms: Optional[float] = None
    stimulus_first_frame_timestamp_ms: Optional[float] = None
    deadline_timestamp_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SsvepTrialWindow":
        identifiers = {
            "model_id": str(data.get("modelId") or "").strip(),
            "run_id": str(data.get("runId") or "").strip(),
            "trial_id": str(data.get("trialId") or "").strip(),
            "attempt_id": str(data.get("attemptId") or "").strip(),
        }
        missing = [name for name, value in identifiers.items() if not value]
        if missing:
            raise ValueError(
                "SSVEP trial window is missing " + ", ".join(name.replace("_", "") for name in missing)
            )
        channel_labels = data.get("channelLabels") or []
        samples = data.get("data") or []
        targets = data.get("targets") or []
        metadata = data.get("metadata") or {}
        if not isinstance(channel_labels, list) or not isinstance(samples, list):
            raise ValueError("SSVEP trial channelLabels and data must be lists")
        if not isinstance(targets, list) or not isinstance(metadata, Mapping):
            raise ValueError("SSVEP trial targets and metadata have invalid types")
        try:
            sample_rate = float(data.get("sampleRate") or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("SSVEP trial sampleRate must be a positive finite number") from exc
        if not math.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("SSVEP trial sampleRate must be a positive finite number")
        if len(channel_labels) != len(samples) or not channel_labels:
            raise ValueError("SSVEP trial channelLabels and data must have the same non-zero channel count")
        channel_data: List[List[float]] = []
        expected_samples: Optional[int] = None
        for channel in samples:
            if not isinstance(channel, list) or not channel:
                raise ValueError("SSVEP trial data must contain non-empty channel arrays")
            if expected_samples is None:
                expected_samples = len(channel)
            elif len(channel) != expected_samples:
                raise ValueError("SSVEP trial data channel lengths must match")
            try:
                values = [float(value) for value in channel]
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("SSVEP trial data must contain finite numbers") from exc
            if any(not math.isfinite(value) for value in values):
                raise ValueError("SSVEP trial data must contain finite numbers")
            channel_data.append(values)
        return cls(
            **identifiers,
            experiment_id=str(data.get("experimentId") or ""),
            sample_rate=sample_rate,
            channel_labels=[str(label) for label in channel_labels],
            data=channel_data,
            targets=[dict(target) for target in targets if isinstance(target, Mapping)],
            attempt=int(data.get("attempt")) if data.get("attempt") is not None else None,
            sample_index_start=data.get("sampleIndexStart"),
            sample_index_end=data.get("sampleIndexEnd"),
            acquisition_sample_start=data.get("acquisitionSampleStart"),
            acquisition_sample_end_exclusive=data.get("acquisitionSampleEndExclusive"),
            recording_sample_start=data.get("recordingSampleStart"),
            recording_sample_end_exclusive=data.get("recordingSampleEndExclusive"),
            window_start_timestamp_ms=data.get("windowStartTimestampMs"),
            window_end_timestamp_ms=data.get("windowEndTimestampMs"),
            stimulus_first_frame_timestamp_ms=data.get("stimulusFirstFrameTimestampMs"),
            deadline_timestamp_ms=data.get("deadlineTimestampMs"),
            metadata=dict(metadata),
            raw=dict(data),
        )

    @property
    def modelId(self) -> str:
        return self.model_id

    @property
    def runId(self) -> str:
        return self.run_id

    @property
    def trialId(self) -> str:
        return self.trial_id

    @property
    def attemptId(self) -> str:
        return self.attempt_id

    @property
    def experimentId(self) -> str:
        return self.experiment_id

    @property
    def sampleRate(self) -> float:
        return self.sample_rate

    @property
    def channelLabels(self) -> List[str]:
        return self.channel_labels


@dataclass
class SsvepPrediction:
    """A target prediction or an explicit abstention from an SSVEP model."""

    target_id: Optional[str] = None
    abstain: bool = False
    scores: Dict[str, float] = field(default_factory=dict)
    confidence: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.target_id = str(self.target_id).strip() if self.target_id is not None else None
        if self.target_id == "":
            self.target_id = None
        if self.abstain and self.target_id:
            raise ValueError("An SSVEP prediction cannot set both target_id and abstain")
        if not self.abstain and not self.target_id:
            raise ValueError("An SSVEP prediction requires target_id or abstain=True")
        self.scores = {str(key): float(value) for key, value in self.scores.items()}
        if self.confidence is not None:
            self.confidence = float(self.confidence)
        self.metadata = dict(self.metadata)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SsvepPrediction":
        scores = data.get("scores") or {}
        metadata = data.get("metadata") or {}
        if not isinstance(scores, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("SSVEP prediction scores and metadata must be objects")
        return cls(
            target_id=data.get("targetId", data.get("target_id")),
            abstain=bool(data.get("abstained", data.get("abstain", False))),
            scores=dict(scores),
            confidence=data.get("confidence"),
            metadata=dict(metadata),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "targetId": self.target_id,
            "abstained": self.abstain,
        }
        if self.scores:
            payload["scores"] = dict(self.scores)
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @property
    def targetId(self) -> Optional[str]:
        return self.target_id
