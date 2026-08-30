#!/usr/bin/env python3
"""Negative and positive tests for the fail-closed wireless-image gate."""

from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from verify_image import (
    EXPECTED_WIFI_DISABLED_EXPRESSION,
    EXPECTED_WIFI_EMPTY_KEY_EXPRESSION,
    Report,
    verify_wireless_safe_default,
)


class WirelessGateTest(unittest.TestCase):
    def run_gate(self, config: str | None, *, generator: bool = True) -> Report:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        if config is not None:
            path = root / "etc/config/wireless"
            path.parent.mkdir(parents=True)
            path.write_text(config)
        generator_data = (
            EXPECTED_WIFI_DISABLED_EXPRESSION + "\n" +
            EXPECTED_WIFI_EMPTY_KEY_EXPRESSION + "\n"
        ).encode()
        if generator:
            generator_path = root / "lib/wifi/mac80211.uc"
            generator_path.parent.mkdir(parents=True)
            generator_path.write_bytes(generator_data)
        report = Report(Path("fixture-sysupgrade.bin"))
        with patch("verify_image.EXPECTED_WIFI_GENERATOR_SHA256", sha256(generator_data).hexdigest()):
            verify_wireless_safe_default(root, report)
        return report

    def test_absent_wireless_file_is_safe(self) -> None:
        self.assertTrue(self.run_gate(None).ok)

    def test_absent_generator_is_rejected(self) -> None:
        self.assertFalse(self.run_gate(None, generator=False).ok)

    def test_even_disabled_preseed_is_rejected_to_avoid_mutating_network_policy(self) -> None:
        report = self.run_gate("""
config wifi-device 'radio0'
        option disabled '1'
config 'wifi-device' "radio1"
        option disabled "true"
config wifi-iface 'default_radio0'
        option device 'radio0'
        option mode 'ap'
""")
        self.assertFalse(report.ok)

    def test_omitted_disabled_is_rejected(self) -> None:
        report = self.run_gate("""
config wifi-device 'radio0'
        option type 'mac80211'
""")
        self.assertFalse(report.ok)

    def test_explicit_enabled_device_is_rejected(self) -> None:
        report = self.run_gate("""
config wifi-device 'radio0'
        option disabled '0'
""")
        self.assertFalse(report.ok)

    def test_key_is_rejected_even_when_device_is_disabled(self) -> None:
        report = self.run_gate("""
config wifi-device 'radio0'
        option disabled 'yes'
config wifi-iface 'default_radio0'
        option device 'radio0'
        option key 'must-never-ship'
""")
        self.assertFalse(report.ok)

    def test_existing_empty_file_is_rejected_fail_closed(self) -> None:
        self.assertFalse(self.run_gate("# no auditable device sections\n").ok)

    def test_dangling_wireless_symlink_is_rejected(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        generator_data = (
            EXPECTED_WIFI_DISABLED_EXPRESSION + "\n" +
            EXPECTED_WIFI_EMPTY_KEY_EXPRESSION + "\n"
        ).encode()
        generator_path = root / "lib/wifi/mac80211.uc"
        generator_path.parent.mkdir(parents=True)
        generator_path.write_bytes(generator_data)
        wireless = root / "etc/config/wireless"
        wireless.parent.mkdir(parents=True)
        wireless.symlink_to("/tmp/nonexistent-wireless")
        report = Report(Path("fixture-sysupgrade.bin"))
        with patch("verify_image.EXPECTED_WIFI_GENERATOR_SHA256",
                   sha256(generator_data).hexdigest()):
            verify_wireless_safe_default(root, report)
        self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()
