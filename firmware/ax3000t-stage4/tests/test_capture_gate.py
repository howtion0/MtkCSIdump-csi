#!/usr/bin/env python3
"""Negative controls for the CSI service's effective default state."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from verify_image import Report, verify_capture_default_off


ROOT = Path(__file__).resolve().parents[1]


class CaptureDefaultGateTest(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = root / "etc/config/mtkcsi"
        init = root / "etc/init.d/mtkcsi-dump"
        config.parent.mkdir(parents=True)
        init.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "package/mtkcsi-dump/files/mtkcsi.config", config)
        shutil.copyfile(ROOT / "package/mtkcsi-dump/files/mtkcsi-dump.init", init)
        init.chmod(0o755)
        return temp, root

    def run_gate(self, root: Path) -> Report:
        report = Report(Path("fixture-sysupgrade.bin"))
        verify_capture_default_off(root, report)
        return report

    def test_exact_disabled_config_and_init_pass(self) -> None:
        _, root = self.fixture()
        self.assertTrue(self.run_gate(root).ok)

    def test_later_enabled_one_overrides_zero_and_is_rejected(self) -> None:
        _, root = self.fixture()
        config = root / "etc/config/mtkcsi"
        config.write_text(config.read_text() + "\toption enabled '1'\n")
        report = self.run_gate(root)
        self.assertFalse(report.ok)
        gate = next(gate for gate in report.gates if gate.name == "rootfs.capture.disabled")
        self.assertEqual(gate.evidence["effective_enabled"], "1")

    def test_nonexecutable_init_is_rejected(self) -> None:
        _, root = self.fixture()
        (root / "etc/init.d/mtkcsi-dump").chmod(0o644)
        self.assertFalse(self.run_gate(root).ok)


if __name__ == "__main__":
    unittest.main()
