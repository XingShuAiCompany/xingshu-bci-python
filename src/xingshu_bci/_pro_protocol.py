"""XingShuN1 Pro UDP wire format (EEG1 / EGS1 / EGK1 / EGC1 / EGH1).

Byte-for-byte port of the platform's ``scripts/xingshu-n1-pro.js``; see
``docs/XINGSHUN1PRO_UDP.md`` for the field tables. All integers are
big-endian. Sample decoding is vectorised with numpy so 16 kHz x 32 channels
stays cheap in Python.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

DATA_MAGIC = b"EEG1"
STATUS_MAGIC = b"EGS1"
SETTINGS_MAGIC = b"EGK1"
TEST_MAGIC = b"EGC1"
HEARTBEAT_MAGIC = b"EGH1"

DATA_PORT = 9000
CONTROL_PORT = 9001
HEADER_BYTES = 64
FRAME_BYTES = 116
FRAME_WORDS = 58
STATUS_BYTES = 64
SETTINGS_BYTES = 64
CHANNEL_COUNT = 32
BOARD_COUNT = 4
CHANNELS_PER_BOARD = 8
ADS_BLOCK_BYTES = 28
MAX_UDP_DATAGRAM_BYTES = 1472
MAX_SAMPLE_COUNT = (MAX_UDP_DATAGRAM_BYTES - HEADER_BYTES) // FRAME_BYTES

#: ADS1299 CONFIG1 data-rate codes 0..6.
SAMPLE_RATES_HZ = (16000, 8000, 4000, 2000, 1000, 500, 250)
GAINS = (1, 2, 4, 6, 8, 12, 24)
INPUT_TYPES = ("normal", "shorted", "bias-meas", "mvdd", "temperature", "test", "bias-drp", "bias-drn")
ADS_REFERENCE_VOLTS = 4.5
ADS_POSITIVE_FULL_SCALE = 0x800000

HEARTBEAT_LEASE_MS = 3000
TEST_LEASE_MS = 5000

SETTINGS_RESULT_MESSAGES = {
    2: "设备拒绝了无效的通道设置（增益或输入类型超出范围）。",
    3: "设备会话已变化，设置未应用，请重试。",
}

# EEG1 header flags
FLAG_TEST_MODE = 0x01
FLAG_TEST_SUPPORTED = 0x02
FLAG_BIAS_DISABLED = 0x04
FLAG_BOARD_MASK_VALID = 0x08
FLAG_SETTINGS_SUPPORTED = 0x10


class ProProtocolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def uv_per_count(gain: int) -> float:
    """Microvolts per ADC count for an ADS1299 gain (4.5 V reference)."""
    return ADS_REFERENCE_VOLTS / (gain or 24) / ADS_POSITIVE_FULL_SCALE * 1e6


def control_checksum(data: bytes) -> int:
    """16-bit one's-complement Internet checksum over big-endian words."""
    total = 0
    for (word,) in struct.iter_unpack(">H", data):
        total += word
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


@dataclass
class ProChannel:
    """One channel's hardware settings.

    ``enabled`` maps to the ADS1299 power-down bit, ``gain`` to the PGA,
    ``input_type`` to the input mux, and ``bias`` to whether the channel feeds
    the right-leg drive (BIAS) common-mode estimate.
    """

    enabled: bool = True
    gain: int = 24
    input_type: str = "normal"
    bias: bool = False

    def __post_init__(self) -> None:
        if int(self.gain) not in GAINS:
            raise ValueError(f"增益必须是 {GAINS} 之一，收到 {self.gain!r}")
        if self.input_type not in INPUT_TYPES:
            raise ValueError(f"输入类型必须是 {INPUT_TYPES} 之一，收到 {self.input_type!r}")
        self.gain = int(self.gain)
        self.enabled = bool(self.enabled)
        self.bias = bool(self.bias)

    def chset(self) -> int:
        """CHnSET register byte (SRB2 is never set: the pin is unconnected)."""
        return (0 if self.enabled else 0x80) | (GAINS.index(self.gain) << 4) | INPUT_TYPES.index(self.input_type)

    @classmethod
    def from_chset(cls, value: int, bias: bool = False) -> "ProChannel":
        gain_code = (value >> 4) & 7
        input_code = value & 7
        return cls(
            enabled=(value & 0x80) == 0,
            gain=GAINS[gain_code] if gain_code < len(GAINS) else 24,
            input_type=INPUT_TYPES[input_code],
            bias=bias,
        )


@dataclass
class ProStatus:
    """Decoded EGS1 status datagram (sent once per second and after every restart)."""

    session_id: int
    request_id: int
    result: int
    sample_rate: Optional[int]
    channels: List[ProChannel]
    bias_mask: List[int]
    bias_amplifier_on: bool
    adapter_present: bool
    charging: bool
    battery_valid: bool
    battery_mv: int
    features: int


@dataclass
class ProDataPacket:
    """Decoded EEG1 datagram. ``counts`` is (32, n) int32 raw ADC counts."""

    sequence: int
    flags: int
    first_sample_index: int
    session_id: int
    timestamp_us: int
    sample_rate: int
    counts: np.ndarray
    online_board_mask: int
    diagnostics: Dict[str, int] = field(default_factory=dict)

    @property
    def sample_count(self) -> int:
        return int(self.counts.shape[1])

    @property
    def test_mode(self) -> bool:
        return bool(self.flags & FLAG_TEST_MODE)

    @property
    def test_supported(self) -> bool:
        return bool(self.flags & FLAG_TEST_SUPPORTED)

    @property
    def settings_supported(self) -> bool:
        return bool(self.flags & FLAG_SETTINGS_SUPPORTED)


def decode_data(packet: bytes, *, validate_status: bool = True) -> ProDataPacket:
    n_bytes = len(packet)
    if n_bytes < HEADER_BYTES:
        raise ProProtocolError("short-header", f"packet is only {n_bytes} bytes")
    if packet[:4] != DATA_MAGIC:
        raise ProProtocolError("magic", "packet magic is not EEG1")
    if packet[4] != 1:
        raise ProProtocolError("version", f"unsupported EEG1 version {packet[4]}")
    if packet[5] != HEADER_BYTES:
        raise ProProtocolError("header-length", f"expected {HEADER_BYTES} header bytes, got {packet[5]}")
    (sample_count, sequence, sample_rate, frame_words, flags, first_index, session_id, timestamp_us) = struct.unpack_from(
        ">HIIHHIII", packet, 6
    )
    if not 1 <= sample_count <= MAX_SAMPLE_COUNT:
        raise ProProtocolError("sample-count", f"invalid sample count {sample_count}")
    if sample_rate not in SAMPLE_RATES_HZ:
        raise ProProtocolError("sample-rate", f"unsupported sample rate {sample_rate} Hz")
    if frame_words != FRAME_WORDS:
        raise ProProtocolError("frame-words", f"expected {FRAME_WORDS}, got {frame_words}")
    expected = HEADER_BYTES + sample_count * FRAME_BYTES
    if n_bytes != expected:
        raise ProProtocolError("packet-length", f"expected {expected} bytes for {sample_count} samples, got {n_bytes}")
    diag = struct.unpack_from(">IIIIHHIII", packet, 32)
    diagnostics = {
        "fpgaOverrunCount": diag[0], "fpgaFrameErrors": diag[1], "fpgaTimeouts": diag[2], "txErrors": diag[3],
        "fpgaStatus": diag[4], "phyBsr": diag[5], "dmaErrorSticky": diag[6], "maxPending": diag[7], "totalFrames": diag[8],
    }

    frames = np.frombuffer(packet, dtype=np.uint8, offset=HEADER_BYTES).reshape(sample_count, FRAME_BYTES)
    if (
        np.any(frames[:, 0] != 0xAA) or np.any(frames[:, 1] != 0xBB)
        or np.any(frames[:, -2] != 0x55) or np.any(frames[:, -1] != 0x55)
    ):
        raise ProProtocolError("frame-markers", "invalid frame markers")
    blocks = frames[:, 2:2 + BOARD_COUNT * ADS_BLOCK_BYTES].reshape(sample_count, BOARD_COUNT, ADS_BLOCK_BYTES)
    if validate_status and np.any((blocks[:, :, 0] & 0xF0) != 0xC0):
        raise ProProtocolError("ads-status", "invalid ADS1299 status prefix")
    raw = blocks[:, :, 3:3 + CHANNELS_PER_BOARD * 3].reshape(sample_count, BOARD_COUNT, CHANNELS_PER_BOARD, 3).astype(np.int32)
    counts = (raw[..., 0] << 16) | (raw[..., 1] << 8) | raw[..., 2]
    counts = np.where(counts & 0x800000, counts - 0x1000000, counts).reshape(sample_count, CHANNEL_COUNT)
    online = (flags >> 8) & 0xF if flags & FLAG_BOARD_MASK_VALID else 0xF
    return ProDataPacket(
        sequence=sequence, flags=flags, first_sample_index=first_index, session_id=session_id,
        timestamp_us=timestamp_us, sample_rate=sample_rate, counts=np.ascontiguousarray(counts.T),
        online_board_mask=online, diagnostics=diagnostics,
    )


def decode_status(packet: bytes) -> ProStatus:
    if len(packet) != STATUS_BYTES or packet[:4] != STATUS_MAGIC:
        raise ProProtocolError("status-format", "invalid EGS1 status packet")
    if packet[4] != 1 or struct.unpack_from(">H", packet, 6)[0] != STATUS_BYTES:
        raise ProProtocolError("status-version", f"unsupported EGS1 version {packet[4]}")
    if control_checksum(packet) != 0:
        raise ProProtocolError("status-checksum", "EGS1 checksum mismatch")
    session_id, request_id = struct.unpack_from(">II", packet, 8)
    bias_mask = list(packet[50:54])
    channels = [
        ProChannel.from_chset(packet[18 + i], bool(bias_mask[i >> 3] & (1 << (i & 7))))
        for i in range(CHANNEL_COUNT)
    ]
    flags = packet[55]
    rate_code = packet[17]
    return ProStatus(
        session_id=session_id,
        request_id=request_id,
        result=packet[16],
        sample_rate=SAMPLE_RATES_HZ[rate_code] if rate_code < len(SAMPLE_RATES_HZ) else None,
        channels=channels,
        bias_mask=bias_mask,
        bias_amplifier_on=packet[54] != 0,
        adapter_present=bool(flags & 1),
        charging=bool(flags & 2),
        battery_valid=bool(flags & 4),
        battery_mv=struct.unpack_from(">H", packet, 56)[0],
        features=struct.unpack_from(">H", packet, 58)[0],
    )


def _finish(packet: bytearray, checksum_offset: int) -> bytes:
    struct.pack_into(">H", packet, checksum_offset, control_checksum(bytes(packet)))
    return bytes(packet)


def encode_settings(session_id: int, request_id: int, sample_rate: int, channels: Sequence[ProChannel]) -> bytes:
    if sample_rate not in SAMPLE_RATES_HZ:
        raise ValueError(f"采样率必须是 {sorted(SAMPLE_RATES_HZ)} 之一，收到 {sample_rate!r}")
    if len(channels) != CHANNEL_COUNT:
        raise ValueError(f"需要 {CHANNEL_COUNT} 个通道设置，收到 {len(channels)}")
    packet = bytearray(SETTINGS_BYTES)
    packet[0:4] = SETTINGS_MAGIC
    packet[4] = 1
    packet[5] = 1
    struct.pack_into(">HII", packet, 6, SETTINGS_BYTES, session_id & 0xFFFFFFFF, request_id & 0xFFFFFFFF)
    packet[16] = SAMPLE_RATES_HZ.index(sample_rate)
    for index, channel in enumerate(channels):
        packet[18 + index] = channel.chset()
        if channel.bias and channel.enabled:
            packet[50 + (index >> 3)] |= 1 << (index & 7)
    return _finish(packet, 62)


def _encode_lease(magic: bytes, session_id: int, enabled: bool, lease_ms: int) -> bytes:
    packet = bytearray(16)
    packet[0:4] = magic
    packet[4] = 1
    packet[5] = 1 if enabled else 0
    struct.pack_into(">HIH", packet, 6, 16, session_id & 0xFFFFFFFF, lease_ms if enabled else 0)
    return _finish(packet, 14)


def encode_test_control(session_id: int, enabled: bool) -> bytes:
    """EGC1: internal square-wave test; the device drops it unless renewed within 5 s."""
    return _encode_lease(TEST_MAGIC, session_id, enabled, TEST_LEASE_MS)


def encode_heartbeat(session_id: int, acquiring: bool) -> bytes:
    """EGH1: host is acquiring (makes the data LED blink); 3 s lease."""
    return _encode_lease(HEARTBEAT_MAGIC, session_id, acquiring, HEARTBEAT_LEASE_MS)
