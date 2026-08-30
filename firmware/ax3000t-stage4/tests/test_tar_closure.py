#!/usr/bin/env python3
"""Negative controls for bytes hidden after tar EOF and before fwtool."""

from __future__ import annotations

import io
import json
import struct
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path

from verify_image import EXPECTED_BOARD, Report, read_sysupgrade


def append_info(image: bytes) -> bytes:
    metadata = {
        "metadata_version": "1.1",
        "compat_version": "2.0",
        "new_supported_devices": ["xiaomi,mi-router-ax3000t"],
        "supported_devices": ["legacy warning fixture"],
        "version": {"target": "mediatek/filogic", "board": EXPECTED_BOARD},
    }
    payload = struct.pack(">II", 0, 0) + json.dumps(
        metadata, separators=(",", ":")
    ).encode()
    body = image + payload
    crc = (zlib.crc32(body) ^ 0xFFFFFFFF) & 0xFFFFFFFF
    return body + struct.pack(">4sIB3xI", b"FWx0", crc, 1, len(payload) + 16)


def sysupgrade_tar() -> bytes:
    root = f"sysupgrade-{EXPECTED_BOARD}"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as tf:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tf.addfile(directory)
        for leaf, data in (
            ("CONTROL", f"BOARD={EXPECTED_BOARD}\n".encode()),
            ("kernel", b"kernel"),
            ("root", b"root"),
        ):
            member = tarfile.TarInfo(f"{root}/{leaf}")
            member.size = len(data)
            member.mode = 0o644
            tf.addfile(member, io.BytesIO(data))
    return output.getvalue()


class TarClosureGateTest(unittest.TestCase):
    def run_gate(self, image: bytes) -> Report:
        report = Report(Path("fixture.bin"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.bin"
            path.write_bytes(image)
            read_sysupgrade(path, report)
        return report

    def test_canonical_tar_padding_passes(self) -> None:
        report = self.run_gate(append_info(sysupgrade_tar()))
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "sysupgrade.tar.closure"],
            ["pass"],
        )

    def test_hidden_blob_before_valid_info_is_rejected(self) -> None:
        image = append_info(sysupgrade_tar() + b"ROUTER_CALIBRATION_SECRET_BYTES")
        report = self.run_gate(image)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "sysupgrade.tar.closure"],
            ["fail"],
        )


if __name__ == "__main__":
    unittest.main()
