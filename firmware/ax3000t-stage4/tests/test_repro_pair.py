#!/usr/bin/env python3
"""Negative controls for the mandatory two-clean-build byte identity gate."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/compare_repro_builds.py"
sys.path.insert(0, str(ROOT / "scripts"))
from compare_repro_builds import COMPARE_FILES, IMAGE  # noqa: E402


class ReproPairTest(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        first, second = Path(temp.name) / "first", Path(temp.name) / "second"
        first.mkdir()
        second.mkdir()
        for directory in (first, second):
            for name in COMPARE_FILES:
                (directory / name).write_bytes(("same:" + name + "\n").encode())
            (directory / "build.log").write_text("diagnostic log may differ\n")
            (directory / "build-provenance.json").write_text(json.dumps({
                "stage4_source_commit": "a" * 40,
                "source_date_epoch": 1782770622,
                "builder": {
                    "image_id": "sha256:" + "b" * 64,
                    "base_digest": "ubuntu@sha256:" + "c" * 64,
                },
                "audit_report_sha256": {},
                "publication_ready": False,
            }) + "\n")
        return temp, first, second

    def run_gate(self, first: Path, second: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--first", str(first), "--second", str(second)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )

    def test_two_identical_clean_outputs_finalize_canonical(self) -> None:
        _, first, second = self.fixture()
        result = self.run_gate(first, second)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((first / "reproducibility-gates.json").read_text())
        provenance = json.loads((first / "build-provenance.json").read_text())
        self.assertEqual(report["result"], "pass")
        self.assertTrue(provenance["publication_ready"])
        self.assertEqual(provenance["reproducibility_clean_builds"], 2)

    def test_one_byte_difference_blocks_finalization(self) -> None:
        _, first, second = self.fixture()
        (second / IMAGE).write_bytes((second / IMAGE).read_bytes() + b"x")
        result = self.run_gate(first, second)
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((first / "reproducibility-gates.json").read_text())
        provenance = json.loads((first / "build-provenance.json").read_text())
        self.assertEqual(report["result"], "fail")
        self.assertFalse(provenance["publication_ready"])

    def test_one_byte_vanilla_module_difference_blocks_finalization(self) -> None:
        _, first, second = self.fixture()
        name = "mt7915e.vanilla.ko"
        payload = (second / name).read_bytes()
        (second / name).write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        result = self.run_gate(first, second)
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((first / "reproducibility-gates.json").read_text())
        self.assertEqual(report["result"], "fail")
        failed = {gate["name"] for gate in report["gates"] if gate["status"] == "fail"}
        self.assertIn("repro.byte_identity.mt7915e.vanilla.ko", failed)


if __name__ == "__main__":
    unittest.main()
