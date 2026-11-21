"""Direct XingShuN1 Pro acquisition, without the desktop platform.

The N1 Pro streams over Ethernet UDP: data and status are broadcast to UDP
9000, and settings, square-wave test and host heartbeat go to UDP 9001. This
module talks to the device directly, so a script gets the full-rate signal
(up to 16 kHz x 32 channels) and can change every hardware setting.

Quick start::

    from xingshu_bci.pro import XingShuN1Pro

    with XingShuN1Pro(sample_rate=1000) as dev:          # connect, set 1 kHz
        dev.configure_channels(range(1, 9), gain=24, bias=True)
        data = dev.read(seconds=2)                        # (32, 2000) microvolts
        print(data.shape, dev.battery)

Requires numpy (``python -m pip install ".[device]"``). Do not run the desktop
platform's acquisition at the same time: both would control the same device.
"""

from __future__ import annotations

import collections
import sys
import socket
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Deque, Dict, Iterable, Iterator, List, Optional, Sequence, Union

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised only without numpy
    raise ImportError(
        'xingshu_bci.pro 需要 numpy，请运行：python -m pip install ".[device]"'
    ) from exc

from ._battery import BatteryEstimator
from ._pro_protocol import (
    CHANNEL_COUNT,
    CONTROL_PORT,
    DATA_MAGIC,
    DATA_PORT,
    GAINS,
    INPUT_TYPES,
    SAMPLE_RATES_HZ,
    SETTINGS_RESULT_MESSAGES,
    STATUS_MAGIC,
    ProChannel,
    ProDataPacket,
    ProProtocolError,
    ProStatus,
    decode_data,
    decode_status,
    encode_heartbeat,
    encode_settings,
    encode_test_control,
    uv_per_count,
)
from .exceptions import XingShuConnectionError, XingShuError, XingShuTimeoutError
from .models import BatteryStatus

__all__ = [
    "XingShuN1Pro",
    "ProChannel",
    "ProChunk",
    "ProStatus",
    "SAMPLE_RATES",
    "GAINS",
    "INPUT_TYPES",
    "CHANNEL_NAMES",
]

#: Selectable sample rates in Hz, ascending.
SAMPLE_RATES = tuple(sorted(SAMPLE_RATES_HZ))
#: Channel order of every array: A1..A8, B1..B8, C1..C8, D1..D8 (= electrode connector pins 2..33).
CHANNEL_NAMES = tuple(f"{chip}{n}" for chip in "ABCD" for n in range(1, 9))

_RECEIVE_BUFFER_BYTES = 4 * 1024 * 1024
_CONTROL_INTERVAL_S = 1.0


@dataclass
class ProChunk:
    """A block of samples as it arrived from the device."""

    data: "np.ndarray"          # (32, n) float64 microvolts
    sample_rate: int
    first_sample_index: int     # device sample counter of data[:, 0]
    session_id: int
    received_at: float          # host time.time() when the datagram arrived

    @property
    def sample_count(self) -> int:
        return int(self.data.shape[1])


@dataclass
class _Pending:
    request_id: int
    from_session: int
    sample_rate: int
    uv: "np.ndarray"
    acked: bool = False
    done: bool = False
    error: Optional[BaseException] = None


def _ipv4_to_int(address: str) -> int:
    return int.from_bytes(socket.inet_aton(address), "big")


def _int_to_ipv4(value: int) -> str:
    return socket.inet_ntoa((value & 0xFFFFFFFF).to_bytes(4, "big"))


def _windows_ipv4_table() -> List[tuple]:
    """(address, netmask) of every connected IPv4 address, via iphlpapi.GetIpAddrTable.

    The standard library hides link-local (169.254.x.x) addresses on Windows,
    and the N1 Pro lives on exactly such a network.
    """
    import ctypes
    from ctypes import wintypes

    class MIB_IPADDRROW(ctypes.Structure):
        _fields_ = [
            ("dwAddr", wintypes.DWORD), ("dwIndex", wintypes.DWORD), ("dwMask", wintypes.DWORD),
            ("dwBCastAddr", wintypes.DWORD), ("dwReasmSize", wintypes.DWORD),
            ("unused1", ctypes.c_ushort), ("wType", ctypes.c_ushort),
        ]

    get_table = ctypes.windll.iphlpapi.GetIpAddrTable
    size = wintypes.ULONG(0)
    get_table(None, ctypes.byref(size), False)
    buffer = ctypes.create_string_buffer(size.value or 4096)
    if get_table(buffer, ctypes.byref(size), False) != 0:
        return []
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buffer) + ctypes.sizeof(wintypes.DWORD), ctypes.POINTER(MIB_IPADDRROW))
    result = []
    for i in range(count):
        row = rows[i]
        if row.wType & 0x0008:  # MIB_IPADDR_DISCONNECTED
            continue
        # dwAddr/dwMask are in network byte order stored in a little-endian DWORD.
        address = socket.inet_ntoa(int(row.dwAddr).to_bytes(4, "little"))
        mask = socket.inet_ntoa(int(row.dwMask).to_bytes(4, "little"))
        if not address.startswith("127.") and address != "0.0.0.0":
            result.append((address, mask))
    return result


def _local_ipv4_table() -> List[tuple]:
    if sys.platform == "win32":
        try:
            return _windows_ipv4_table()
        except (OSError, AttributeError, ValueError):
            return []
    return []


def _channel_index(channel: Union[int, str]) -> int:
    """1-based channel number or name (``"A1"``..``"D8"``) -> 0-based index."""
    if isinstance(channel, str):
        name = channel.strip().upper()
        if name in CHANNEL_NAMES:
            return CHANNEL_NAMES.index(name)
        if name.isdigit():
            channel = int(name)
        else:
            raise ValueError(f"未知通道 {channel!r}，应为 1..32 或 A1..D8")
    index = int(channel) - 1
    if not 0 <= index < CHANNEL_COUNT:
        raise ValueError(f"通道号必须在 1..32 之间，收到 {channel!r}")
    return index


class XingShuN1Pro:
    """Direct UDP connection to one XingShuN1 Pro.

    Args:
        sample_rate: Rate to switch the device to on :meth:`connect`
            (250, 500, 1000, 2000, 4000, 8000 or 16000 Hz). ``None`` keeps the
            device's current rate.
        bind_address: Local address to receive on; ``0.0.0.0`` listens on every
            network adapter.
        port: UDP data port (device default 9000).
        control_port: UDP control port on the device (default 9001).
        control_address: Where to send control packets. By default the
            directed broadcast of the adapter that reaches the device.
        buffer_seconds: How much unread data to keep; older samples are dropped
            and counted in ``stats["overflowSamples"]``.
        link_timeout: Seconds without data before the link counts as lost
            (device off or cable out).
    """

    SAMPLE_RATES = SAMPLE_RATES
    GAINS = GAINS
    INPUT_TYPES = INPUT_TYPES
    CHANNEL_COUNT = CHANNEL_COUNT
    CHANNEL_NAMES = CHANNEL_NAMES

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        *,
        bind_address: str = "0.0.0.0",
        port: int = DATA_PORT,
        control_port: int = CONTROL_PORT,
        control_address: Optional[str] = None,
        buffer_seconds: float = 10.0,
        link_timeout: float = 5.0,
    ) -> None:
        if sample_rate is not None and int(sample_rate) not in SAMPLE_RATES:
            raise ValueError(f"采样率必须是 {SAMPLE_RATES} 之一，收到 {sample_rate!r}")
        self._requested_rate = int(sample_rate) if sample_rate is not None else None
        self.bind_address = bind_address
        self.port = int(port)
        self.control_port = int(control_port)
        self.control_address = control_address
        self.buffer_seconds = float(buffer_seconds)
        self.link_timeout = float(link_timeout)

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._sock: Optional[socket.socket] = None
        self._threads: List[threading.Thread] = []
        self._closing = threading.Event()

        self._device_address: Optional[str] = None
        self._session: Optional[int] = None
        self._sample_rate: Optional[int] = None
        self._flags = 0
        self._online_mask = 0xF
        self._diagnostics: Dict[str, int] = {}
        self._status: Optional[ProStatus] = None
        self._status_uv: Optional[np.ndarray] = None
        self._status_session: Optional[int] = None
        self._pending: Optional[_Pending] = None
        self._test_desired = False
        self._test_pending: Optional[bool] = None
        self._request_seq = (int(time.time() * 1000) & 0xFFFF) << 8
        self._battery_est = BatteryEstimator(cell_count=2)
        self._battery: Optional[BatteryStatus] = None

        self._acquiring = False
        self._queue: Deque[ProChunk] = collections.deque()
        self._queued = 0
        self._recent: Deque[ProChunk] = collections.deque()
        self._recent_samples = 0
        self._last_data_at: Optional[float] = None
        self._last_status_at: Optional[float] = None
        self._link_lost: Optional[str] = None
        self._expected_index: Optional[int] = None
        self._last_sequence: Optional[int] = None
        self._stats = {
            "packets": 0, "dataPackets": 0, "statusPackets": 0, "samples": 0,
            "missingSamples": 0, "missingFrames": 0, "protocolErrors": 0, "overflowSamples": 0,
        }

    # ── lifecycle ───────────────────────────────────────────────────────

    def connect(self, sample_rate: Optional[int] = None, *, timeout: float = 5.0) -> "XingShuN1Pro":
        """Start receiving, wait for the device, and apply ``sample_rate``.

        Raises :class:`XingShuConnectionError` when no device data arrives
        within ``timeout`` seconds.
        """
        if sample_rate is not None:
            if int(sample_rate) not in SAMPLE_RATES:
                raise ValueError(f"采样率必须是 {SAMPLE_RATES} 之一，收到 {sample_rate!r}")
            self._requested_rate = int(sample_rate)
        if self._sock is None:
            self._open_socket()
        deadline = time.monotonic() + timeout
        found = True
        with self._cond:
            while self._last_data_at is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    found = False
                    break
                self._cond.wait(min(remaining, 0.2))
        if not found:
            # Close outside the lock: close() joins the receive thread, which
            # may be waiting for that lock to store a status packet.
            self.close()
            raise XingShuConnectionError(
                f"{timeout:g} 秒内没有收到 XingShuN1 Pro 的数据。请确认设备已开机、网线已接好、"
                f"电脑有线网卡是 169.254.x.x 地址，并在防火墙中允许 Python 接收 UDP {self.port}。",
                code="DEVICE_NOT_FOUND",
            )
        with self._cond:
            settings_supported = bool(self._flags & 0x10)
            status_deadline = time.monotonic() + 3.0
            while settings_supported and self._status is None and time.monotonic() < status_deadline:
                self._cond.wait(0.2)
        rate = self._requested_rate
        if rate is not None and rate != self._sample_rate:
            self.apply_settings(sample_rate=rate)
        return self

    def close(self) -> None:
        """Stop acquisition, release the square-wave test and the socket."""
        if self._sock is None:
            return
        try:
            if self._test_desired:
                self._test_desired = False
                self._send_control(encode_test_control(self._session or 0, False))
            if self._acquiring:
                self._send_control(encode_heartbeat(self._session or 0, False))
        except OSError:
            pass
        self._closing.set()
        with self._cond:
            self._acquiring = False
            self._cond.notify_all()
        sock, self._sock = self._sock, None
        try:
            sock.close()
        except OSError:
            pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._threads = []

    def __enter__(self) -> "XingShuN1Pro":
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        return self._sock is not None and self._last_data_at is not None and self._link_lost is None

    # ── acquisition ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin buffering samples (and make the device's data LED blink).

        :meth:`read` and :meth:`stream` call this automatically.
        """
        self._require_link()
        with self._cond:
            if not self._acquiring:
                self._clear_buffers()
                self._acquiring = True
        self._send_heartbeat()

    def stop(self) -> None:
        """Stop buffering; unread samples are discarded."""
        with self._cond:
            was = self._acquiring
            self._acquiring = False
            self._clear_buffers()
        if was:
            self._send_control_quiet(encode_heartbeat(self._session or 0, False))

    def read(
        self,
        n_samples: Optional[int] = None,
        *,
        seconds: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> "np.ndarray":
        """Return the next ``n_samples`` (or ``seconds``) of data as (32, n) microvolts.

        Blocks until enough samples have arrived. Samples are consumed: the
        next call continues where this one stopped, with no gap or overlap.
        """
        if not self._acquiring:
            self.start()
        rate = self.sample_rate or 1
        if n_samples is None:
            if seconds is None:
                raise ValueError("请指定 n_samples 或 seconds")
            n_samples = max(1, int(round(seconds * rate)))
        n_samples = int(n_samples)
        if timeout is None:
            timeout = max(2.0, n_samples / rate * 2 + self.link_timeout)
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queued < n_samples:
                self._raise_if_lost()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise XingShuTimeoutError(
                        f"等待 {n_samples} 个样本超时（已收到 {self._queued} 个）。", code="READ_TIMEOUT"
                    )
                self._cond.wait(min(remaining, 0.2))
            return self._take(n_samples)

    def read_available(self) -> "np.ndarray":
        """Return every unread sample immediately, as (32, n); n may be 0."""
        if not self._acquiring:
            self.start()
        with self._cond:
            self._raise_if_lost()
            return self._take(self._queued)

    def get_latest(self, seconds: float) -> "np.ndarray":
        """Return the most recent ``seconds`` of data without consuming it."""
        if not self._acquiring:
            self.start()
        with self._cond:
            want = int(round(seconds * (self.sample_rate or 1)))
            parts: List[np.ndarray] = []
            have = 0
            for chunk in reversed(self._recent):
                parts.append(chunk.data)
                have += chunk.sample_count
                if have >= want:
                    break
        if not parts:
            return np.zeros((CHANNEL_COUNT, 0))
        data = np.concatenate(parts[::-1], axis=1)
        return data[:, -want:] if want else data[:, :0]

    def stream(self, *, timeout: Optional[float] = None) -> Iterator[ProChunk]:
        """Yield chunks of full-rate data as they arrive (about 1–12 samples each).

        Stops when the iterator is closed, the device is closed, or no data
        arrives for ``timeout`` seconds (default: the link timeout, raising).
        """
        if not self._acquiring:
            self.start()
        wait_s = self.link_timeout if timeout is None else timeout
        while True:
            with self._cond:
                deadline = time.monotonic() + wait_s
                while not self._queue:
                    self._raise_if_lost()
                    if self._sock is None:
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise XingShuTimeoutError("数据流超时，没有收到新数据。", code="STREAM_TIMEOUT")
                    self._cond.wait(min(remaining, 0.2))
                chunk = self._queue.popleft()
                self._queued -= chunk.sample_count
            yield chunk

    @property
    def available(self) -> int:
        """Number of unread samples in the buffer."""
        with self._lock:
            return self._queued

    # ── hardware settings ──────────────────────────────────────────────

    @property
    def sample_rate(self) -> Optional[int]:
        """Current device sample rate in Hz."""
        return self._sample_rate

    @property
    def channels(self) -> List[ProChannel]:
        """Device-confirmed settings of the 32 channels (copies)."""
        with self._lock:
            if self._status is None:
                return [ProChannel() for _ in range(CHANNEL_COUNT)]
            return [replace(ch) for ch in self._status.channels]

    @property
    def bias_amplifier_on(self) -> bool:
        """Whether chip D's right-leg drive amplifier is on (any channel has BIAS)."""
        with self._lock:
            return bool(self._status and self._status.bias_amplifier_on)

    @property
    def settings_supported(self) -> bool:
        return bool(self._flags & 0x10)

    def set_sample_rate(self, sample_rate: int, *, timeout: float = 4.0) -> None:
        """Switch the device sample rate; keeps every channel setting."""
        self.apply_settings(sample_rate=sample_rate, timeout=timeout)

    def configure_channels(
        self,
        channels: Optional[Iterable[Union[int, str]]] = None,
        *,
        enabled: Optional[bool] = None,
        gain: Optional[int] = None,
        input_type: Optional[str] = None,
        bias: Optional[bool] = None,
        timeout: float = 4.0,
    ) -> List[ProChannel]:
        """Change some settings of some channels in one device update.

        ``channels`` lists 1-based numbers (``range(1, 9)``) or names
        (``["A1", "B3"]``); ``None`` means all 32. Settings left as ``None``
        keep their current value.

        Example: only A1–A8 on, gain 24, all in BIAS::

            dev.configure_channels(enabled=False, bias=False)        # everything off
            dev.configure_channels(range(1, 9), enabled=True, gain=24, bias=True)
        """
        targets = range(CHANNEL_COUNT) if channels is None else sorted({_channel_index(c) for c in channels})
        updated = self.channels
        for index in targets:
            current = updated[index]
            updated[index] = ProChannel(
                enabled=current.enabled if enabled is None else enabled,
                gain=current.gain if gain is None else gain,
                input_type=current.input_type if input_type is None else input_type,
                bias=current.bias if bias is None else bias,
            )
        self.apply_settings(channels=updated, timeout=timeout)
        return self.channels

    def apply_settings(
        self,
        *,
        sample_rate: Optional[int] = None,
        channels: Optional[Sequence[ProChannel]] = None,
        timeout: float = 4.0,
    ) -> None:
        """Write sample rate and all 32 channel settings to the device at once.

        The device restarts acquisition with a new session (about 0.3 s); the
        unread buffer is cleared so data at different rates or gains never mix.
        """
        self._require_link()
        rate = int(sample_rate) if sample_rate is not None else self._sample_rate
        if rate not in SAMPLE_RATES:
            raise ValueError(f"采样率必须是 {SAMPLE_RATES} 之一，收到 {sample_rate!r}")
        if not self.settings_supported:
            if channels is None and rate == self._sample_rate:
                return
            raise XingShuError("当前 Pro 固件不支持硬件设置和切换采样率，请先升级固件。", code="UNSUPPORTED_FIRMWARE")
        if self._test_desired:
            raise XingShuError("方波测试期间不能修改硬件设置，请先关闭方波测试。", code="TEST_ACTIVE")
        next_channels = list(channels) if channels is not None else self.channels
        if len(next_channels) != CHANNEL_COUNT:
            raise ValueError(f"需要 {CHANNEL_COUNT} 个通道设置，收到 {len(next_channels)}")
        with self._cond:
            if self._pending is not None:
                raise XingShuError("上一组设置还在应用中，请稍候。", code="SETTINGS_BUSY")
            self._request_seq = (self._request_seq + 1) & 0xFFFFFFFF or 1
            pending = _Pending(
                request_id=self._request_seq,
                from_session=self._session or 0,
                sample_rate=rate,
                uv=np.array([uv_per_count(ch.gain) for ch in next_channels]),
            )
            self._pending = pending
        packet = encode_settings(pending.from_session, pending.request_id, rate, next_channels)
        try:
            self._send_control(packet)
            deadline = time.monotonic() + timeout
            with self._cond:
                while not pending.done:
                    self._raise_if_lost()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise XingShuTimeoutError("设备未确认新的设置，请检查网线后重试。", code="SETTINGS_TIMEOUT")
                    self._cond.wait(min(remaining, 0.2))
                if pending.error is not None:
                    raise pending.error
        finally:
            with self._cond:
                if self._pending is pending:
                    self._pending = None

    @property
    def square_wave(self) -> bool:
        """Whether the device reports the internal square-wave test as active."""
        return bool(self._flags & 0x01)

    def set_square_wave(self, enabled: bool, *, timeout: float = 4.0) -> None:
        """Turn the ADS1299 internal test signal on or off for all channels.

        While on, every channel shows a ~1.95 Hz square wave of about 3.75 mV
        peak to peak. The SDK renews the device's 5 s lease every second;
        :meth:`close` turns the test off.
        """
        self._require_link()
        if not self._flags & 0x02:
            raise XingShuError("当前 Pro 固件不支持在线方波测试。", code="UNSUPPORTED_FIRMWARE")
        enabled = bool(enabled)
        with self._cond:
            self._test_desired = enabled
            self._test_pending = enabled
        self._send_control(encode_test_control(self._session or 0, enabled))
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.square_wave != enabled:
                self._raise_if_lost()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._test_desired = False
                    self._test_pending = None
                    self._send_control_quiet(encode_test_control(self._session or 0, False))
                    raise XingShuTimeoutError("设备未确认方波测试状态，请检查网线。", code="TEST_TIMEOUT")
                self._cond.wait(min(remaining, 0.2))
            self._test_pending = None

    # ── status ─────────────────────────────────────────────────────────

    @property
    def device_address(self) -> Optional[str]:
        return self._device_address

    @property
    def status(self) -> Optional[ProStatus]:
        """Last EGS1 status packet (settings, battery pins), sent once per second."""
        return self._status

    @property
    def battery(self) -> Optional[BatteryStatus]:
        """Filtered battery state; ``state == "full"`` only when CHGOK# says so."""
        return self._battery

    @property
    def online_chips(self) -> str:
        """ADS1299 chips that answered at start-up, e.g. ``"ABCD"``."""
        return "".join(chip for bit, chip in enumerate("ABCD") if self._online_mask & (1 << bit))

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                **self._stats,
                "deviceAddress": self._device_address,
                "sessionId": self._session,
                "sampleRate": self._sample_rate,
                "onlineChips": self.online_chips,
                "linkLost": self._link_lost is not None,
                "lastDataAgeS": None if self._last_data_at is None else time.monotonic() - self._last_data_at,
                "diagnostics": dict(self._diagnostics),
            }

    # ── internals ──────────────────────────────────────────────────────

    def _open_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RECEIVE_BUFFER_BYTES)
        except OSError:
            pass
        try:
            sock.bind((self.bind_address, self.port))
        except OSError as exc:
            sock.close()
            raise XingShuConnectionError(f"无法监听 UDP {self.port}：{exc}", code="BIND_FAILED") from exc
        sock.settimeout(0.2)
        with self._cond:
            # Fresh per-connection state, so connect() after close() waits for the device again.
            self._device_address = None
            self._session = None
            self._status = None
            self._status_uv = None
            self._last_data_at = None
            self._last_status_at = None
            self._link_lost = None
            self._expected_index = None
            self._last_sequence = None
            self._battery_est.reset()
            self._battery = None
            self._clear_buffers()
        self._sock = sock
        self._closing.clear()
        self._threads = [
            threading.Thread(target=self._receive_loop, name="xingshu-pro-rx", daemon=True),
            threading.Thread(target=self._control_loop, name="xingshu-pro-ctl", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _receive_loop(self) -> None:
        while not self._closing.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                packet, (address, _port) = sock.recvfrom(2048)
            except socket.timeout:
                self._check_link()
                continue
            except OSError:
                if self._closing.is_set():
                    return
                continue
            self._stats["packets"] += 1
            try:
                if packet[:4] == DATA_MAGIC:
                    self._handle_data(decode_data(packet), address)
                elif packet[:4] == STATUS_MAGIC:
                    self._handle_status(decode_status(packet), address)
            except ProProtocolError:
                self._stats["protocolErrors"] += 1
            self._check_link()

    def _handle_status(self, status: ProStatus, address: str) -> None:
        with self._cond:
            if self._device_address and address != self._device_address:
                return
            self._stats["statusPackets"] += 1
            self._status = status
            self._last_status_at = time.monotonic()
            self._status_session = status.session_id
            self._status_uv = np.array([uv_per_count(ch.gain) for ch in status.channels])
            pending = self._pending
            if pending is not None and status.request_id == pending.request_id:
                if status.result != 0:
                    pending.error = XingShuError(
                        SETTINGS_RESULT_MESSAGES.get(status.result, f"设备拒绝设置（代码 {status.result}）。"),
                        code="SETTINGS_REJECTED",
                    )
                    pending.done = True
                else:
                    pending.acked = True
            estimate = self._battery_est.update(
                status.battery_mv if status.battery_valid else None,
                valid=status.battery_valid,
                adapter_present=status.adapter_present,
                charging=status.charging,
            )
            self._battery = BatteryStatus.from_dict({**estimate, "receivedAtMs": time.time() * 1000})
            self._cond.notify_all()

    def _uv_for_session(self, session_id: int) -> "np.ndarray":
        pending = self._pending
        if pending is not None and session_id != pending.from_session:
            return pending.uv
        if self._status_uv is not None:
            return self._status_uv
        return np.full(CHANNEL_COUNT, uv_per_count(24))

    def _handle_data(self, pkt: ProDataPacket, address: str) -> None:
        now = time.monotonic()
        with self._cond:
            if self._device_address is None:
                self._device_address = address
            elif address != self._device_address:
                return
            session_changed = pkt.session_id != self._session
            rate_changed = pkt.sample_rate != self._sample_rate
            self._session = pkt.session_id
            self._sample_rate = pkt.sample_rate
            self._flags = pkt.flags
            self._online_mask = pkt.online_board_mask
            self._diagnostics = pkt.diagnostics
            self._last_data_at = now
            if self._link_lost is not None:
                self._link_lost = None
            self._stats["dataPackets"] += 1
            self._stats["samples"] += pkt.sample_count

            pending = self._pending
            if (
                pending is not None and pending.acked and not pending.done
                and pkt.session_id != pending.from_session and pkt.sample_rate == pending.sample_rate
            ):
                pending.done = True
                self._clear_buffers()
            elif session_changed and rate_changed:
                self._clear_buffers()
            if session_changed:
                self._expected_index = None
                self._last_sequence = None
            if self._expected_index is not None:
                missing = (pkt.first_sample_index - self._expected_index) & 0xFFFFFFFF
                if 0 < missing < 0x80000000:
                    self._stats["missingSamples"] += missing
            self._expected_index = (pkt.first_sample_index + pkt.sample_count) & 0xFFFFFFFF
            if self._last_sequence is not None:
                delta = (pkt.sequence - self._last_sequence) & 0xFFFFFFFF
                if 1 < delta < 0x80000000:
                    self._stats["missingFrames"] += delta - 1
            self._last_sequence = pkt.sequence

            if self._acquiring:
                uv = self._uv_for_session(pkt.session_id)
                chunk = ProChunk(
                    data=pkt.counts * uv[:, None],
                    sample_rate=pkt.sample_rate,
                    first_sample_index=pkt.first_sample_index,
                    session_id=pkt.session_id,
                    received_at=time.time(),
                )
                self._push(chunk)
            self._cond.notify_all()

    def _push(self, chunk: ProChunk) -> None:
        limit = max(1, int(self.buffer_seconds * chunk.sample_rate))
        self._queue.append(chunk)
        self._queued += chunk.sample_count
        while self._queued > limit and len(self._queue) > 1:
            dropped = self._queue.popleft()
            self._queued -= dropped.sample_count
            self._stats["overflowSamples"] += dropped.sample_count
        self._recent.append(chunk)
        self._recent_samples += chunk.sample_count
        while self._recent_samples > limit and len(self._recent) > 1:
            self._recent_samples -= self._recent.popleft().sample_count

    def _clear_buffers(self) -> None:
        self._queue.clear()
        self._queued = 0
        self._recent.clear()
        self._recent_samples = 0

    def _take(self, n: int) -> "np.ndarray":
        if n <= 0:
            return np.zeros((CHANNEL_COUNT, 0))
        parts: List[np.ndarray] = []
        need = n
        while need > 0:
            chunk = self._queue[0]
            if chunk.sample_count <= need:
                self._queue.popleft()
                parts.append(chunk.data)
                need -= chunk.sample_count
            else:
                parts.append(chunk.data[:, :need])
                self._queue[0] = replace(
                    chunk,
                    data=chunk.data[:, need:],
                    first_sample_index=(chunk.first_sample_index + need) & 0xFFFFFFFF,
                )
                need = 0
        self._queued -= n
        return np.concatenate(parts, axis=1) if len(parts) > 1 else np.array(parts[0])

    def _check_link(self) -> None:
        if self._last_data_at is None or self._link_lost is not None:
            return
        if self._pending is not None:
            return
        silent = time.monotonic() - self._last_data_at
        if silent < self.link_timeout:
            return
        status_alive = self._last_status_at is not None and time.monotonic() - self._last_status_at < self.link_timeout
        with self._cond:
            self._link_lost = (
                f"XingShuN1 Pro 已 {silent:.0f} 秒没有采样数据（设备仍在发送状态包），请检查采集板供电和 FPGA。"
                if status_alive else
                f"XingShuN1 Pro 已 {silent:.0f} 秒没有任何数据，设备可能已关机或网线已断开。"
            )
            self._cond.notify_all()

    def _raise_if_lost(self) -> None:
        if self._link_lost is not None:
            raise XingShuConnectionError(self._link_lost, code="LINK_LOST")

    def _require_link(self) -> None:
        if self._sock is None or self._last_data_at is None:
            raise XingShuConnectionError("尚未连接设备，请先调用 connect()。", code="NOT_CONNECTED")
        self._raise_if_lost()

    def _control_targets(self) -> List[tuple]:
        """(local address, destination) pairs for control packets.

        The device answers no ARP, so control packets go out as a directed
        broadcast. A PC can have several adapters on the device's subnet
        (Ethernet, Bluetooth PAN and virtual adapters are all 169.254.x.x), and
        the OS route may pick the wrong one, so send on every matching adapter;
        the device sees only the copy on its own cable.
        """
        if self.control_address:
            return [("0.0.0.0", self.control_address)]
        device = self._device_address or "169.254.87.100"
        device_int = _ipv4_to_int(device)
        targets = []
        for address, mask in _local_ipv4_table():
            mask_int = _ipv4_to_int(mask)
            local_int = _ipv4_to_int(address)
            if (local_int & mask_int) == (device_int & mask_int):
                targets.append((address, _int_to_ipv4((local_int & mask_int) | (~mask_int & 0xFFFFFFFF))))
        if targets:
            return targets
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((device, self.control_port))
            local = probe.getsockname()[0]
        except OSError:
            local = "0.0.0.0"
        finally:
            probe.close()
        if local.startswith("169.254.") or device.startswith("169.254."):
            return [(local, "169.254.255.255")]
        return [(local, "255.255.255.255")]

    def _send_control(self, packet: bytes) -> None:
        errors: List[OSError] = []
        targets = self._control_targets()
        for local, target in targets:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind((local, 0))
                sock.sendto(packet, (target, self.control_port))
            except OSError as exc:
                errors.append(exc)
            finally:
                sock.close()
        if errors and len(errors) == len(targets):
            raise errors[0]

    def _send_control_quiet(self, packet: bytes) -> None:
        try:
            self._send_control(packet)
        except OSError:
            pass

    def _send_heartbeat(self) -> None:
        if self._session is not None:
            self._send_control_quiet(encode_heartbeat(self._session, True))

    def _control_loop(self) -> None:
        while not self._closing.wait(_CONTROL_INTERVAL_S):
            if self._session is None or self._link_lost is not None:
                continue
            if self._acquiring:
                self._send_heartbeat()
            if self._test_desired:
                self._send_control_quiet(encode_test_control(self._session, True))
