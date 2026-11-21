"""Direct XingShuN1 Pro access: wire format and behaviour against a fake device."""

from __future__ import annotations

import time
import unittest

import numpy as np

import xingshu_bci
from xingshu_bci import XingShuConnectionError, XingShuError
from xingshu_bci._pro_protocol import (
    ProChannel,
    ProProtocolError,
    control_checksum,
    decode_data,
    decode_status,
    encode_heartbeat,
    encode_settings,
    encode_test_control,
    uv_per_count,
)
from xingshu_bci.pro import CHANNEL_NAMES, SAMPLE_RATES, XingShuN1Pro

from fake_pro import FakePro, free_port, make_data_packet, make_status_packet


class ProtocolTests(unittest.TestCase):
    def test_data_packet_decodes_signed_counts_channel_major(self) -> None:
        pkt = make_data_packet(sequence=7, sample_rate=16000, first_index=100, session=42, count=2,
                               value_fn=lambda s, c: -1 if c == 31 else s * 1000 + c)
        decoded = decode_data(pkt)
        self.assertEqual(decoded.counts.shape, (32, 2))
        self.assertEqual(decoded.counts[0, 0], 100000)
        self.assertEqual(decoded.counts[9, 1], 101009)
        self.assertEqual(decoded.counts[31, 0], -1)
        self.assertEqual((decoded.sequence, decoded.session_id, decoded.first_sample_index), (7, 42, 100))
        self.assertEqual(decoded.online_board_mask, 0xF)
        self.assertTrue(decoded.settings_supported)

    def test_bad_packets_raise_protocol_errors(self) -> None:
        good = make_data_packet(sequence=1, sample_rate=16000, first_index=0, session=1, count=1)
        with self.assertRaises(ProProtocolError):
            decode_data(good[:-1])
        broken = bytearray(good)
        broken[64] = 0
        with self.assertRaises(ProProtocolError):
            decode_data(bytes(broken))
        bad_rate = bytearray(good)
        bad_rate[12:16] = (1234).to_bytes(4, "big")
        with self.assertRaises(ProProtocolError):
            decode_data(bytes(bad_rate))

    def test_settings_packet_matches_platform_encoding(self) -> None:
        # Same vector as scripts/tests/xingshu-n1-pro.test.js.
        channels = [ProChannel(enabled=i != 31, gain=1 if i == 8 else 24,
                               input_type="shorted" if i == 0 else "normal", bias=i < 3) for i in range(32)]
        packet = encode_settings(0x1234, 7, 1000, channels)
        self.assertEqual(packet[:4], b"EGK1")
        self.assertEqual(control_checksum(packet), 0)
        self.assertEqual(int.from_bytes(packet[8:12], "big"), 0x1234)
        self.assertEqual(packet[16], 4)
        self.assertEqual(packet[18], 0x61)
        self.assertEqual(packet[18 + 8], 0x00)
        self.assertEqual(packet[18 + 31], 0xE0)
        self.assertEqual(packet[50], 0x07)
        with self.assertRaises(ValueError):
            encode_settings(1, 1, 1234, channels)
        with self.assertRaises(ValueError):
            ProChannel(gain=3)

    def test_status_packet_round_trip(self) -> None:
        status = decode_status(make_status_packet(session=9, request_id=5, rate_code=4, chsets=[0x61] + [0x60] * 31,
                                                  bias=[0x03, 0, 0, 0], flags=0x07, mv=8100))
        self.assertEqual(status.sample_rate, 1000)
        self.assertEqual(status.channels[0].input_type, "shorted")
        self.assertTrue(status.channels[1].bias)
        self.assertTrue(status.bias_amplifier_on)
        self.assertTrue(status.adapter_present and status.charging and status.battery_valid)
        self.assertEqual(status.battery_mv, 8100)

    def test_lease_packets_checksum(self) -> None:
        for packet in (encode_heartbeat(5, True), encode_test_control(5, True), encode_test_control(5, False)):
            self.assertEqual(len(packet), 16)
            self.assertEqual(control_checksum(packet), 0)

    def test_constants_and_lazy_export(self) -> None:
        self.assertEqual(SAMPLE_RATES, (250, 500, 1000, 2000, 4000, 8000, 16000))
        self.assertEqual(CHANNEL_NAMES[0], "A1")
        self.assertEqual(CHANNEL_NAMES[31], "D8")
        self.assertIs(xingshu_bci.XingShuN1Pro, XingShuN1Pro)
        self.assertAlmostEqual(uv_per_count(24), 4.5 / 24 / 0x800000 * 1e6)


class DeviceTests(unittest.TestCase):
    def open(self, fake: FakePro, **kwargs) -> XingShuN1Pro:
        dev = XingShuN1Pro(bind_address="127.0.0.1", port=fake.data_port, control_port=fake.control_port,
                           control_address="127.0.0.1", **kwargs)
        self.addCleanup(dev.close)
        return dev

    def test_connect_switches_rate_and_reads_full_rate_without_gaps(self) -> None:
        with FakePro(free_port(), sample_rate=16000) as fake:
            dev = self.open(fake, sample_rate=1000).connect(timeout=3)
            self.assertEqual(dev.sample_rate, 1000)
            self.assertEqual(fake.sample_rate, 1000)
            data = dev.read(500)
            self.assertEqual(data.shape, (32, 500))
            more = dev.read(240)
            # Fake counts are index*10 + channel: consecutive reads are contiguous.
            step = (more[0, 0] - data[0, -1]) / uv_per_count(24)
            self.assertAlmostEqual(step, 10, places=3)
            self.assertEqual(dev.stats["missingSamples"], 0)
            self.assertGreater(fake.heartbeats, 0)

    def test_16khz_full_rate_stream(self) -> None:
        with FakePro(free_port(), sample_rate=16000) as fake:
            dev = self.open(fake).connect(timeout=3)
            data = dev.read(seconds=1.0)
            self.assertEqual(data.shape, (32, 16000))
            self.assertEqual(dev.sample_rate, 16000)

    def test_configure_channels_writes_gain_bias_and_rescales(self) -> None:
        with FakePro(free_port(), sample_rate=1000) as fake:
            dev = self.open(fake).connect(timeout=3)
            dev.configure_channels(enabled=False, bias=False)
            dev.configure_channels(range(1, 9), enabled=True, gain=12, bias=True)
            channels = dev.channels
            self.assertTrue(all(ch.enabled and ch.gain == 12 and ch.bias for ch in channels[:8]))
            self.assertTrue(all(not ch.enabled for ch in channels[8:]))
            self.assertTrue(dev.bias_amplifier_on)
            self.assertEqual(fake.bias, [0xFF, 0, 0, 0])
            data = dev.read(24)
            counts = data[0] / uv_per_count(12)
            self.assertTrue(np.allclose(counts, np.round(counts)))
            dev.configure_channels(["B1"], enabled=True, input_type="shorted")
            self.assertEqual(dev.channels[8].input_type, "shorted")

    def test_rejected_settings_raise(self) -> None:
        with FakePro(free_port(), sample_rate=1000) as fake:
            dev = self.open(fake).connect(timeout=3)
            fake.reject_next = True
            with self.assertRaises(XingShuError) as ctx:
                dev.set_sample_rate(250)
            self.assertEqual(ctx.exception.code, "SETTINGS_REJECTED")
            self.assertEqual(dev.sample_rate, 1000)

    def test_square_wave_toggles_and_blocks_settings(self) -> None:
        with FakePro(free_port(), sample_rate=1000) as fake:
            dev = self.open(fake).connect(timeout=3)
            dev.set_square_wave(True)
            self.assertTrue(dev.square_wave)
            with self.assertRaises(XingShuError):
                dev.set_sample_rate(250)
            dev.set_square_wave(False)
            self.assertFalse(dev.square_wave)

    def test_battery_comes_from_status(self) -> None:
        with FakePro(free_port(), sample_rate=1000) as fake:
            dev = self.open(fake).connect(timeout=3)
            deadline = time.time() + 3
            while dev.battery is None and time.time() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(dev.battery)
            self.assertEqual(dev.battery.state, "discharging")
            self.assertEqual(dev.battery.cellCount, 2)

    def test_stream_yields_chunks_and_link_loss_raises(self) -> None:
        with FakePro(free_port(), sample_rate=1000) as fake:
            dev = self.open(fake, link_timeout=0.6).connect(timeout=3)
            total = 0
            for chunk in dev.stream():
                total += chunk.sample_count
                self.assertEqual(chunk.data.shape[0], 32)
                if total >= 120:
                    break
            fake.paused = True
            fake.tx.close()
            with self.assertRaises(XingShuConnectionError) as ctx:
                dev.read(5000, timeout=5)
            self.assertEqual(ctx.exception.code, "LINK_LOST")

    def test_no_device_raises_clear_error(self) -> None:
        dev = XingShuN1Pro(bind_address="127.0.0.1", port=free_port())
        with self.assertRaises(XingShuConnectionError) as ctx:
            dev.connect(timeout=0.5)
        self.assertEqual(ctx.exception.code, "DEVICE_NOT_FOUND")

    def test_reconnect_after_close_waits_for_device_again(self) -> None:
        port = free_port()
        with FakePro(port, sample_rate=1000) as fake:
            dev = self.open(fake).connect(timeout=3)
            dev.read(24)
            dev.close()
        again = XingShuN1Pro(bind_address="127.0.0.1", port=port)
        with self.assertRaises(XingShuConnectionError):
            again.connect(timeout=0.5)
        with self.assertRaises(XingShuConnectionError):
            dev.connect(timeout=0.5)

    def test_invalid_sample_rate_rejected_early(self) -> None:
        with self.assertRaises(ValueError):
            XingShuN1Pro(sample_rate=1234)


if __name__ == "__main__":
    unittest.main()
