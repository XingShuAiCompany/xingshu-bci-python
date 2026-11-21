"""A fake XingShuN1 Pro on localhost for SDK tests.

Sends EEG1 data and EGS1 status to a data port and honours EGK1 / EGC1 / EGH1
on its own control port, like rev2 firmware: applying settings restarts the
session at the new rate and reports the request in the next status packet.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import List, Optional

from xingshu_bci._pro_protocol import (
    FRAME_BYTES,
    HEADER_BYTES,
    SAMPLE_RATES_HZ,
    control_checksum,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def signed24(value: int) -> bytes:
    value &= 0xFFFFFF
    return bytes(((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF))


def make_data_packet(*, sequence: int, sample_rate: int, first_index: int, session: int, count: int,
                     flags: int = 0x18 | (0xF << 8), value_fn=None) -> bytes:
    packet = bytearray(HEADER_BYTES + count * FRAME_BYTES)
    packet[0:4] = b"EEG1"
    packet[4] = 1
    packet[5] = HEADER_BYTES
    struct.pack_into(">HIIHHIII", packet, 6, count, sequence, sample_rate, 58, flags, first_index, session, 1000)
    for s in range(count):
        o = HEADER_BYTES + s * FRAME_BYTES
        packet[o] = 0xAA
        packet[o + 1] = 0xBB
        for board in range(4):
            b = o + 2 + board * 28
            packet[b] = 0xC0
            for ch in range(8):
                channel = board * 8 + ch
                value = value_fn(first_index + s, channel) if value_fn else (first_index + s) * 10 + channel
                packet[b + 3 + ch * 3:b + 6 + ch * 3] = signed24(value)
        packet[o + FRAME_BYTES - 2] = 0x55
        packet[o + FRAME_BYTES - 1] = 0x55
    return bytes(packet)


def make_status_packet(*, session: int, request_id: int = 0, result: int = 0, rate_code: int = 0,
                       chsets: Optional[List[int]] = None, bias: Optional[List[int]] = None,
                       flags: int = 0x04, mv: int = 7800) -> bytes:
    packet = bytearray(64)
    packet[0:4] = b"EGS1"
    packet[4] = 1
    struct.pack_into(">HII", packet, 6, 64, session, request_id)
    packet[16] = result
    packet[17] = rate_code
    for i, value in enumerate(chsets or [0x60] * 32):
        packet[18 + i] = value
    bias = bias or [0, 0, 0, 0]
    packet[50:54] = bytes(bias)
    packet[54] = 1 if any(bias) else 0
    packet[55] = flags
    struct.pack_into(">HH", packet, 56, mv, 1)
    struct.pack_into(">H", packet, 62, control_checksum(bytes(packet)))
    return bytes(packet)


class FakePro:
    def __init__(self, data_port: int, *, sample_rate: int = 16000, packet_samples: int = 12) -> None:
        self.data_port = data_port
        self.sample_rate = sample_rate
        self.packet_samples = packet_samples
        self.session = 0x1000
        self.chsets = [0x60] * 32
        self.bias = [0, 0, 0, 0]
        self.request_id = 0
        self.result = 0
        self.test_mode = False
        self.heartbeats = 0
        self.settings_packets: List[bytes] = []
        self.paused = False
        self.reject_next = False
        self.restarted = False
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctl.bind(("127.0.0.1", 0))
        self.ctl.settimeout(0.05)
        self.control_port = self.ctl.getsockname()[1]
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._send_loop, daemon=True),
                         threading.Thread(target=self._control_loop, daemon=True)]

    def __enter__(self) -> "FakePro":
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self.tx.close()
        self.ctl.close()

    def _status(self) -> bytes:
        return make_status_packet(session=self.session, request_id=self.request_id, result=self.result,
                                  rate_code=SAMPLE_RATES_HZ.index(self.sample_rate), chsets=self.chsets,
                                  bias=self.bias)

    def _send_loop(self) -> None:
        sequence = 0
        index = 0
        start = time.perf_counter()
        sent_samples = 0
        last_status = 0.0
        while not self._stop.is_set():
            now = time.perf_counter()
            if now - last_status >= 1.0:
                self.tx.sendto(self._status(), ("127.0.0.1", self.data_port))
                last_status = now
            if self.restarted:
                # New session: the sample counter and pacing restart at the new rate.
                self.restarted = False
                start, sent_samples, index = time.perf_counter(), 0, 0
                continue
            if self.paused:
                time.sleep(0.01)
                start, sent_samples = time.perf_counter(), 0
                continue
            due = int((now - start) * self.sample_rate)
            while sent_samples + self.packet_samples <= due:
                flags = 0x18 | (0xF << 8) | 0x02 | (0x01 if self.test_mode else 0)
                pkt = make_data_packet(sequence=sequence, sample_rate=self.sample_rate, first_index=index,
                                       session=self.session, count=self.packet_samples, flags=flags)
                self.tx.sendto(pkt, ("127.0.0.1", self.data_port))
                sequence = (sequence + 1) & 0xFFFFFFFF
                index = (index + self.packet_samples) & 0xFFFFFFFF
                sent_samples += self.packet_samples
            time.sleep(0.001)

    def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                packet, _ = self.ctl.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if control_checksum(packet) != 0:
                continue
            magic = packet[:4]
            if magic == b"EGK1":
                self.settings_packets.append(packet)
                self.request_id = struct.unpack_from(">I", packet, 12)[0]
                if self.reject_next:
                    self.reject_next = False
                    self.result = 2
                    self.tx.sendto(self._status(), ("127.0.0.1", self.data_port))
                    continue
                self.result = 0
                self.sample_rate = SAMPLE_RATES_HZ[packet[16]]
                self.chsets = list(packet[18:50])
                self.bias = list(packet[50:54])
                self.session += 1
                self.restarted = True
                time.sleep(0.05)  # acquisition restart
                self.tx.sendto(self._status(), ("127.0.0.1", self.data_port))
            elif magic == b"EGC1":
                self.test_mode = packet[5] == 1
            elif magic == b"EGH1":
                self.heartbeats += 1
