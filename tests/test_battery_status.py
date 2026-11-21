"""BatteryStatus parsing from the platform's ``telemetry.battery``."""

from __future__ import annotations

import unittest

import xingshu_bci
from xingshu_bci import BatteryStatus, ServerStatus


PRO_STATUS = {
    "state": "connected",
    "board": "xingshu_n1_pro",
    "transport": "udp",
    "sampleRate": 1000,
    "channelCount": 32,
    "telemetry": {
        "battery": {
            "valid": True,
            "percent": 64,
            "state": "charging",
            "voltageMv": 7812,
            "rawVoltageMv": 7820,
            "cellMv": 3906,
            "cellCount": 2,
            "charging": True,
            "chargeComplete": False,
            "adapterPresent": True,
            "low": False,
            "critical": False,
            "estimating": False,
            "receivedAtMs": 1794700000000,
        },
        "imu": None,
    },
}


class BatteryStatusTests(unittest.TestCase):
    def test_version_matches_platform_release(self) -> None:
        self.assertEqual(xingshu_bci.__version__, "1.0.0")

    def test_status_exposes_typed_battery(self) -> None:
        status = ServerStatus.from_dict(PRO_STATUS)
        self.assertEqual(status.sampleRate, 1000)
        battery = status.battery
        self.assertIsInstance(battery, BatteryStatus)
        assert battery is not None
        self.assertTrue(battery.valid)
        self.assertEqual(battery.percent, 64)
        self.assertEqual(battery.state, "charging")
        self.assertEqual(battery.voltageMv, 7812)
        self.assertEqual(battery.cellMv, 3906)
        self.assertEqual(battery.cellCount, 2)
        self.assertTrue(battery.charging)
        self.assertTrue(battery.adapterPresent)
        self.assertEqual(battery.raw["rawVoltageMv"], 7820)
        self.assertIn("imu", status.telemetry)

    def test_full_and_unknown_states(self) -> None:
        full = BatteryStatus.from_dict({"valid": True, "percent": 100, "state": "full", "chargeComplete": True})
        self.assertEqual(full.state, "full")
        self.assertTrue(full.chargeComplete)
        unknown = BatteryStatus.from_dict({"valid": False, "percent": None, "state": "unknown", "voltageMv": None})
        self.assertFalse(unknown.valid)
        self.assertIsNone(unknown.percent)
        self.assertIsNone(unknown.voltageMv)

    def test_missing_telemetry_gives_no_battery(self) -> None:
        self.assertIsNone(ServerStatus.from_dict({"state": "idle"}).battery)
        self.assertIsNone(ServerStatus.from_dict({"state": "idle", "telemetry": None}).battery)
        self.assertIsNone(ServerStatus.from_dict({"state": "connected", "telemetry": {"battery": None}}).battery)

    def test_non_numeric_fields_are_ignored(self) -> None:
        battery = BatteryStatus.from_dict({"percent": "64", "voltageMv": True, "adapterPresent": "yes"})
        self.assertIsNone(battery.percent)
        self.assertIsNone(battery.voltageMv)
        self.assertIsNone(battery.adapterPresent)


if __name__ == "__main__":
    unittest.main()
