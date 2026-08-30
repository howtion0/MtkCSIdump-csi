#!/usr/bin/env python3
"""Negative controls for fwtool's newest-first metadata selection."""

from __future__ import annotations

import json
import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from verify_image import (
    EXPECTED_COMPAT_MESSAGE,
    EXPECTED_LEGACY_SUPPORTED_MESSAGE,
    EXPECTED_REVISION,
    EXPECTED_VERSION_DIST,
    EXPECTED_VERSION_NUMBER,
    Report,
    fwtool_chunks,
    verify_image_signature,
    verify_metadata,
)


def append_chunk(image: bytes, kind: int, payload: bytes) -> bytes:
    body = image + payload
    crc = (zlib.crc32(body) ^ 0xFFFFFFFF) & 0xFFFFFFFF
    trailer = struct.pack(">4sIB3xI", b"FWx0", crc, kind, len(payload) + 16)
    return body + trailer


def append_info(image: bytes, *, device: str, compat: str,
                message: str | None, revision: str = EXPECTED_REVISION) -> bytes:
    metadata = {
        "metadata_version": "1.1",
        "new_supported_devices": [device],
        "supported_devices": [EXPECTED_LEGACY_SUPPORTED_MESSAGE],
        "compat_version": compat,
        "compat_message": message,
        "version": {
            "dist": EXPECTED_VERSION_DIST,
            "version": EXPECTED_VERSION_NUMBER,
            "revision": revision,
            "target": "mediatek/filogic",
            "board": "xiaomi_mi-router-ax3000t",
        },
    }
    payload = struct.pack(">II", 0, 0) + json.dumps(
        metadata, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return append_chunk(image, 1, payload)


class ReportSchemaTest(unittest.TestCase):
    def test_public_gate_report_explicitly_denies_flash_authorization(self) -> None:
        report = Report(Path("fixture-sysupgrade.bin"))
        report.passed("fixture", "fixture")
        self.assertIs(report.as_json()["flash_authorized"], False)


class MetadataGateTest(unittest.TestCase):
    def valid(self, image: bytes = b"firmware") -> bytes:
        image = append_info(
            image,
            device="xiaomi,mi-router-ax3000t",
            compat="2.0",
            message=EXPECTED_COMPAT_MESSAGE,
        )
        return append_chunk(image, 0, b"fixture-ucert")

    def run_gate(self, image: bytes) -> tuple[Report, dict | None]:
        report = Report(Path("fixture-sysupgrade.bin"))
        metadata = verify_metadata(image, report)
        return report, metadata

    def test_exact_signature_then_info_layout_passes_structural_gate(self) -> None:
        report, _ = self.run_gate(self.valid())
        self.assertTrue(report.ok)

    def test_new_malicious_info_cannot_hide_behind_old_valid_info(self) -> None:
        image = append_info(
            self.valid(),
            device="wrong,device",
            compat="1.0",
            message=None,
        )
        report, selected = self.run_gate(image)
        self.assertFalse(report.ok)
        self.assertEqual(selected["compat_version"], "1.0")
        self.assertEqual(
            [gate.status for gate in report.gates
             if gate.name == "metadata.fwtool.unique_info"],
            ["fail"],
        )

    def test_duplicate_info_is_rejected_even_when_newest_is_valid(self) -> None:
        old = append_info(b"firmware", device="wrong,device", compat="1.0", message=None)
        old = append_chunk(old, 0, b"old-fixture-ucert")
        report, selected = self.run_gate(self.valid(old))
        self.assertFalse(report.ok)
        self.assertEqual(selected["compat_version"], "2.0")

    def test_duplicate_signature_chunk_is_rejected(self) -> None:
        image = append_chunk(self.valid(), 0, b"second-signature")
        report, _ = self.run_gate(image)
        self.assertFalse(report.ok)

    def test_missing_new_supported_devices_is_rejected(self) -> None:
        image = self.valid()
        # Rebuild the INFO layer without new_supported_devices, then append the
        # structurally expected signature layer.
        metadata = {
            "metadata_version": "1.1",
            "supported_devices": [EXPECTED_LEGACY_SUPPORTED_MESSAGE],
            "compat_version": "2.0",
            "compat_message": EXPECTED_COMPAT_MESSAGE,
            "version": {
                "dist": EXPECTED_VERSION_DIST, "version": EXPECTED_VERSION_NUMBER,
                "revision": EXPECTED_REVISION, "target": "mediatek/filogic",
                "board": "xiaomi_mi-router-ax3000t",
            },
        }
        payload = struct.pack(">II", 0, 0) + json.dumps(
            metadata, separators=(",", ":"), ensure_ascii=False
        ).encode()
        rebuilt = append_chunk(b"firmware", 1, payload)
        rebuilt = append_chunk(rebuilt, 0, b"fixture-ucert")
        report, _ = self.run_gate(rebuilt)
        self.assertFalse(report.ok)

    def test_wrong_source_revision_is_rejected(self) -> None:
        image = append_info(
            b"firmware",
            device="xiaomi,mi-router-ax3000t",
            compat="2.0",
            message=EXPECTED_COMPAT_MESSAGE,
            revision="r-wrong-source",
        )
        image = append_chunk(image, 0, b"fixture-ucert")
        report, _ = self.run_gate(image)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates
             if gate.name == "metadata.version.identity"],
            ["fail"],
        )


class SignatureGateTest(unittest.TestCase):
    fingerprint = "0123456789abcdef"

    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], dict[str, Path], bytes]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "root"
        pub = base / "ax3000t-stage4.pub"
        cert = base / "ax3000t-stage4.ucert"
        ucert = base / "ucert"
        usign = base / "usign"
        lock = base / "source-lock.json"
        pub.write_bytes(b"untrusted comment: fixture\nRWfixture\n")
        cert.write_bytes(b"fixture-base-ucert")
        for executable in (ucert, usign):
            executable.write_text("fixture\n")
            executable.chmod(0o755)
        trusted = root / f"etc/opkg/keys/{self.fingerprint}"
        trusted.parent.mkdir(parents=True)
        trusted.write_bytes(pub.read_bytes())
        lock.write_text(json.dumps({
            "signing": {
                "status": "READY",
                "public_key_sha256": hashlib.sha256(pub.read_bytes()).hexdigest(),
                "base_ucert_sha256": hashlib.sha256(cert.read_bytes()).hexdigest(),
                "usign_fingerprint": self.fingerprint,
                "base_ucert_validfrom": 1788126829,
                "base_ucert_expiresat": 1819662829,
            }
        }))
        image = MetadataGateTest().valid()
        signed_prefix = image[:fwtool_chunks(image)[0]["start"]]
        image = append_chunk(signed_prefix, 0, cert.read_bytes() + b"-per-image")
        return temp, {
            "root": root, "pub": pub, "cert": cert, "ucert": ucert,
            "usign": usign, "lock": lock,
        }, image

    def run_gate(self, paths: dict[str, Path], image: bytes,
                 expected_prefix: bytes) -> Report:
        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            if "-F" in command:
                return SimpleNamespace(returncode=0, stdout=self.fingerprint + "\n")
            if "-D" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout='"validfrom": 1788126829,\n"expiresat": 1819662829\n',
                )
            if "-V" in command and "-m" not in command:
                return SimpleNamespace(returncode=0, stdout="")
            message = Path(command[command.index("-m") + 1]).read_bytes()
            return SimpleNamespace(
                returncode=0 if message == expected_prefix else 1,
                stdout="",
            )

        report = Report(Path("fixture.bin"))
        with patch("verify_image.subprocess.run", side_effect=fake_run):
            verify_image_signature(
                image, paths["root"], ucert=paths["ucert"], usign=paths["usign"],
                public_key=paths["pub"], base_ucert=paths["cert"],
                source_lock=paths["lock"], report=report,
            )
        return report

    def test_locked_key_crypto_and_root_trust_pass(self) -> None:
        _, paths, image = self.fixture()
        expected = image[:fwtool_chunks(image)[0]["start"]]
        self.assertTrue(self.run_gate(paths, image, expected).ok)

    def test_resigned_structure_without_matching_crypto_is_rejected(self) -> None:
        _, paths, image = self.fixture()
        expected = image[:fwtool_chunks(image)[0]["start"]]
        changed = MetadataGateTest().valid(b"changed-firmware")
        self.assertFalse(self.run_gate(paths, changed, expected).ok)

    def test_rootfs_key_must_byte_match_pinned_public_key(self) -> None:
        _, paths, image = self.fixture()
        (paths["root"] / f"etc/opkg/keys/{self.fingerprint}").write_bytes(b"wrong")
        expected = image[:fwtool_chunks(image)[0]["start"]]
        self.assertFalse(self.run_gate(paths, image, expected).ok)

    def test_base_ucert_validity_must_match_lock(self) -> None:
        _, paths, image = self.fixture()
        lock = json.loads(paths["lock"].read_text())
        lock["signing"]["base_ucert_expiresat"] += 1
        paths["lock"].write_text(json.dumps(lock))
        expected = image[:fwtool_chunks(image)[0]["start"]]
        report = self.run_gate(paths, image, expected)
        self.assertFalse(report.ok)
        self.assertEqual(
            [gate.status for gate in report.gates
             if gate.name == "signature.base_ucert_identity"],
            ["fail"],
        )


if __name__ == "__main__":
    unittest.main()
