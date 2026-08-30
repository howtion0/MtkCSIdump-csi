#!/usr/bin/env python3
"""Negative controls for selected FIT payloads and the exact DT layout."""

from __future__ import annotations

import copy
import hashlib
import lzma
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from verify_image import (
    EXPECTED_PARTITIONS,
    EXPECTED_PARTITIONS_PATH,
    Report,
    verify_dtb,
)


def string(value: str) -> bytes:
    return value.encode() + b"\0"


def direct_reference_hashes(path: str, data: bytes) -> dict[str, dict[str, bytes]]:
    return {
        f"{path}/hash-1": {
            "algo": string("crc32"),
            "value": struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF),
        },
        f"{path}/hash-2": {
            "algo": string("sha1"),
            "value": hashlib.sha1(data).digest(),
        },
    }


class FitGateTest(unittest.TestCase):
    def fixture(self) -> tuple[dict[str, dict[str, bytes]], dict[str, dict[str, bytes]]]:
        arm64 = bytearray(512)
        arm64[56:60] = b"ARMd"
        arm64[80:80 + len(b"Linux version 6.12.94 (builder@buildhost) fixture\0")] = \
            b"Linux version 6.12.94 (builder@buildhost) fixture\0"
        kernel = lzma.compress(bytes(arm64), format=lzma.FORMAT_ALONE)
        dtb_payload = b"inner-dtb-fixture"
        fit = {
            "/configurations": {"default": string("config-1")},
            "/configurations/config-1": {
                "kernel": string("kernel-1"),
                "fdt": string("fdt-1"),
            },
            "/images/kernel-1": {
                "description": string("ARM64 OpenWrt Linux-6.12.94"),
                "data": kernel,
                "type": string("kernel"),
                "arch": string("arm64"),
                "os": string("linux"),
                "compression": string("lzma"),
                "load": struct.pack(">Q", 0x48000000),
                "entry": struct.pack(">Q", 0x48000000),
            },
            "/images/fdt-1": {
                "description": string(
                    "ARM64 OpenWrt xiaomi_mi-router-ax3000t device tree blob"
                ),
                "data": dtb_payload,
                "type": string("flat_dt"),
                "arch": string("arm64"),
                "compression": string("none"),
            },
        }
        fit.update(direct_reference_hashes("/images/kernel-1", kernel))
        fit.update(direct_reference_hashes("/images/fdt-1", dtb_payload))
        dtb: dict[str, dict[str, bytes]] = {
            "/": {"compatible": string("xiaomi,mi-router-ax3000t")},
            "/soc/spi@1100a000": {
                "compatible": string("mediatek,mt7981-spi-ipm"),
                "status": string("okay"),
                "#address-cells": (1).to_bytes(4, "big"),
                "#size-cells": (0).to_bytes(4, "big"),
            },
            "/soc/spi@1100a000/flash@0": {
                "compatible": string("spi-nand"),
                "reg": (0).to_bytes(4, "big"),
                "mediatek,nmbm": b"",
                "mediatek,bmt-max-ratio": (1).to_bytes(4, "big"),
                "mediatek,bmt-max-reserved-blocks": (64).to_bytes(4, "big"),
                "mediatek,bmt-mtd-overridden-oobsize": (64).to_bytes(4, "big"),
            },
            EXPECTED_PARTITIONS_PATH: {
                "compatible": string("fixed-partitions"),
                "#address-cells": (1).to_bytes(4, "big"),
                "#size-cells": (1).to_bytes(4, "big"),
            },
        }
        for node, label, offset, size, read_only in EXPECTED_PARTITIONS:
            props = {
                "label": string(label),
                "reg": offset.to_bytes(4, "big") + size.to_bytes(4, "big"),
            }
            if read_only:
                props["read-only"] = b""
            dtb[f"{EXPECTED_PARTITIONS_PATH}/{node}"] = props
        return fit, dtb

    def run_gate(self, fit: dict[str, dict[str, bytes]],
                 dtb: dict[str, dict[str, bytes]]) -> Report:
        report = Report(Path("fixture-sysupgrade.bin"))
        with patch("verify_image.parse_fdt", side_effect=[fit, dtb]):
            verify_dtb(b"outer-fit-fixture", report)
        return report

    def test_exact_selected_payloads_and_layout_pass(self) -> None:
        fit, dtb = self.fixture()
        self.assertTrue(self.run_gate(fit, dtb).ok)

    def test_hash_on_dummy_image_cannot_substitute_for_selected_dtb_hash(self) -> None:
        fit, dtb = self.fixture()
        fit.pop("/images/fdt-1/hash-1")
        fit.pop("/images/fdt-1/hash-2")
        dummy = b"dummy"
        fit["/images/dummy"] = {"data": dummy, "type": string("firmware")}
        fit.update(direct_reference_hashes("/images/dummy", dummy))
        report = self.run_gate(fit, dtb)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "fit.dtb.selected_hash"],
            ["fail"],
        )

    def test_unselected_flat_dt_cannot_replace_missing_default_fdt(self) -> None:
        fit, dtb = self.fixture()
        fit["/configurations/config-1"].pop("fdt")
        report = self.run_gate(fit, dtb)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "fit.dtb.selection"],
            ["fail"],
        )

    def test_empty_lzma_kernel_is_rejected(self) -> None:
        fit, dtb = self.fixture()
        kernel = lzma.compress(b"", format=lzma.FORMAT_ALONE)
        fit["/images/kernel-1"]["data"] = kernel
        fit.update(direct_reference_hashes("/images/kernel-1", kernel))
        self.assertFalse(self.run_gate(fit, dtb).ok)

    def test_lzma_trailing_data_is_rejected(self) -> None:
        fit, dtb = self.fixture()
        kernel = fit["/images/kernel-1"]["data"] + b"trailing"
        fit["/images/kernel-1"]["data"] = kernel
        fit.update(direct_reference_hashes("/images/kernel-1", kernel))
        self.assertFalse(self.run_gate(fit, dtb).ok)

    def test_wrong_kernel_load_or_entry_is_rejected(self) -> None:
        for field in ("load", "entry"):
            with self.subTest(field=field):
                fit, dtb = self.fixture()
                fit["/images/kernel-1"][field] = struct.pack(">Q", 0x48001000)
                self.assertFalse(self.run_gate(fit, dtb).ok)

    def test_nested_hash_node_cannot_authenticate_selected_payload(self) -> None:
        fit, dtb = self.fixture()
        nested = fit.pop("/images/fdt-1/hash-2")
        fit["/images/fdt-1/nested/hash-2"] = nested
        report = self.run_gate(fit, dtb)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "fit.payload.hashes"],
            ["fail"],
        )

    def test_wrong_fdt_identity_is_rejected(self) -> None:
        fit, dtb = self.fixture()
        fit["/images/fdt-1"]["description"] = string("generic device tree")
        report = self.run_gate(fit, dtb)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates if gate.name == "fit.dtb.selection"],
            ["fail"],
        )

    def test_partition_read_only_mismatch_is_rejected(self) -> None:
        fit, dtb = self.fixture()
        dtb = copy.deepcopy(dtb)
        dtb[f"{EXPECTED_PARTITIONS_PATH}/partition@0"].pop("read-only")
        self.assertFalse(self.run_gate(fit, dtb).ok)


if __name__ == "__main__":
    unittest.main()
