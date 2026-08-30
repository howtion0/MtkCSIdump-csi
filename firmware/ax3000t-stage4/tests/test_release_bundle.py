#!/usr/bin/env python3
"""Hardware-free tests for the public GitHub Release allowlist."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare_release_bundle.py"
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_release_bundle import (  # noqa: E402
    AUDIT_ASSETS,
    INTERNAL_PASS_REPORTS,
    FINAL_REQUIRED_ARTIFACTS,
    LOCKED_BASE_DIGEST,
    REPORT_SPECS,
    TOOLING_FILES,
)

IMAGE = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseBundleTest(unittest.TestCase):
    def fixture(self, base: Path) -> tuple[Path, Path]:
        source = base / "out"
        bundle = base / "bundle"
        source.mkdir()
        image = b"generic-source-built-test-image\n"
        (source / IMAGE).write_bytes(image)
        for name in AUDIT_ASSETS:
            path = source / name
            if not path.exists():
                path.write_bytes(("fixture:" + name + "\n").encode())
        public_key = (source / "ax3000t-stage4.pub").read_bytes()
        base_ucert = (source / "ax3000t-stage4.ucert").read_bytes()
        fingerprint = "0123456789abcdef"
        signing_lock = json.loads((ROOT / "source-lock.json").read_text())
        locked_builder = signing_lock["builder"]
        signing_lock["signing"] = {
            "status": "READY",
            "public_key_sha256": digest(public_key),
            "base_ucert_sha256": digest(base_ucert),
            "usign_fingerprint": fingerprint,
            "base_ucert_validfrom": 1788126829,
            "base_ucert_expiresat": 1819662829,
        }
        (source / "source-lock.json").write_text(json.dumps(signing_lock) + "\n")
        def make_report(name: str) -> dict:
            spec = REPORT_SPECS[name]
            gates = []
            for gate_name in sorted(spec["gates"]):
                gate = {"name": gate_name, "status": "pass"}
                if "detail" in spec["gate_fields"]:
                    gate.update({"detail": "fixture", "evidence": None})
                else:
                    gate.update({"expected": "fixture", "actual": "fixture"})
                gates.append(gate)
            report = {
                "schema": 1,
                "classification": spec["classification"],
                "result": "pass",
                "gates": gates,
            }
            if "phase" in spec:
                report["phase"] = spec["phase"]
            return report

        gate = make_report("gate-report.json")
        gate.update({
            "image": IMAGE,
            "flash_authorized": False,
            "artifacts": {},
        })
        gate["artifacts"] = {name: {"fixture": True} for name in FINAL_REQUIRED_ARTIFACTS}
        gate["artifacts"]["image"] = {"sha256": digest(image), "bytes": len(image)}
        gate["artifacts"]["signature"] = {
            "signed_prefix_bytes": len(image),
            "public_key_sha256": digest(public_key),
            "base_ucert_sha256": digest(base_ucert),
            "usign_fingerprint": fingerprint,
            "base_ucert_validfrom": 1788126829,
            "base_ucert_expiresat": 1819662829,
            "signed_prefix_sha256": "e" * 64,
            "signature_chunk_bytes": 128,
            "signature_chunk_sha256": "f" * 64,
        }
        (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
        for name in INTERNAL_PASS_REPORTS:
            report = make_report(name)
            if name.startswith("source-"):
                report.update({"source_lock": "source-lock.json", "source_lock_sha256": "c" * 64})
            elif name == "capture-source-gates.json":
                report.update({
                    "archive": "capture.tar.gz", "bytes": 123, "expected_bytes": 123,
                    "sha256": "d" * 64, "expected_sha256": "d" * 64,
                })
            elif name == "vanilla-abi-gates.json":
                report["purpose"] = "vanilla-before-CSI ABI gate"
            elif name == "reproducibility-gates.json":
                report.update({
                    "clean_build_count": 2,
                    "image": IMAGE,
                    "image_sha256": digest(image),
                })
            (source / name).write_text(json.dumps(report) + "\n")
        report_names = ("gate-report.json",) + INTERNAL_PASS_REPORTS
        provenance = json.loads((ROOT / "manifest.template.json").read_text())
        provenance.update({
            "gate_result": "pass",
            "publication_ready": True,
            "reproducibility_pending": False,
            "flash_authorized": False,
            "stage4_source_commit": "a" * 40,
            "stage4_source_tree": "b" * 40,
            "stage4_source_archive_sha256": "c" * 64,
            "image": {"name": IMAGE, "bytes": len(image), "sha256": digest(image)},
            "gate_report_sha256": digest((source / "gate-report.json").read_bytes()),
            "audit_report_sha256": {
                name: digest((source / name).read_bytes()) for name in report_names
            },
            "tooling_sha256": {
                name: digest((ROOT / name).read_bytes()) for name in TOOLING_FILES
            },
            "signature": gate["artifacts"]["signature"],
            "reproducibility_clean_builds": 2,
            "reproducibility_gate_sha256": digest(
                (source / "reproducibility-gates.json").read_bytes()
            ),
            "reproducibility_second_image_sha256": digest(image),
            "capture_source_gate_sha256": digest(
                (source / "capture-source-gates.json").read_bytes()
            ),
            "source_lock_sha256": digest((source / "source-lock.json").read_bytes()),
            "kwrt_exact_config_sha256": digest((source / "kwrt-exact.config").read_bytes()),
            "build_config_sha256": digest((source / "build.config").read_bytes()),
            "kernel_config_sha256": digest((source / "kernel.config").read_bytes()),
            "module_symvers_sha256": digest((source / "Module.symvers").read_bytes()),
            "build_log_sha256": digest((source / "build.log").read_bytes()),
            "package_manifest_sha256": digest((source / "packages.manifest").read_bytes()),
        })
        provenance["builder"].update({
            "base_digest": LOCKED_BASE_DIGEST,
            "image_id": "sha256:" + "b" * 64,
            "jobs": 2,
            "source_date_epoch": locked_builder["source_date_epoch"],
            "apt_snapshot": locked_builder["apt_snapshot"],
            "dockerfile_sha256": locked_builder["dockerfile_sha256"],
            "apt_sources_sha256": locked_builder["apt_sources_sha256"],
            "package_versions_sha256": digest((source / "builder-packages.txt").read_bytes()),
            "package_versions": (source / "builder-packages.txt").read_text().splitlines(),
            "networked_prepare_receipt_sha256": digest(
                (source / "network-prepare-receipt.json").read_bytes()
            ),
            "download_manifest_sha256": digest(
                (source / "download-closure.json").read_bytes()
            ),
            "build_network": "none",
        })
        (source / "build-provenance.json").write_text(json.dumps(provenance) + "\n")
        hashes = []
        for name in (IMAGE, "gate-report.json", "build-provenance.json"):
            hashes.append(f"{digest((source / name).read_bytes())}  {name}")
        (source / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
        audit_hashes = [
            f"{digest((source / name).read_bytes())}  {name}" for name in AUDIT_ASSETS
        ]
        (source / "AUDIT-SHA256SUMS").write_text("\n".join(audit_hashes) + "\n")
        return source, bundle

    def run_bundle(self, source: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--build-output", str(source),
             "--bundle", str(bundle)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_copies_only_four_allowlisted_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            # A private-looking neighbor proves the implementation uses no glob.
            (source / "mtd0-factory-backup.bin").write_bytes(b"must not escape")
            result = self.run_bundle(source, bundle)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path.name for path in bundle.iterdir()},
                {IMAGE, "SHA256SUMS", "build-provenance.json", "gate-report.json"},
            )

    def test_rejects_nonpass_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            gate = json.loads((source / "gate-report.json").read_text())
            gate["gates"][0]["status"] = "warn"
            (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-pass gates", result.stderr)

    def test_rejects_gate_report_without_flash_authorization_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            gate = json.loads((source / "gate-report.json").read_text())
            del gate["flash_authorized"]
            (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("top-level schema differs", result.stderr)

    def test_rejects_gate_report_that_authorizes_flashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            gate = json.loads((source / "gate-report.json").read_text())
            gate["flash_authorized"] = True
            (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must explicitly deny flash authorization", result.stderr)

    def test_rejects_local_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["kernel_build_identity"] = "/Users/example/private"
            data = (json.dumps(provenance) + "\n").encode()
            (source / "build-provenance.json").write_bytes(data)
            lines = (source / "SHA256SUMS").read_text().splitlines()
            lines = [
                f"{digest(data)}  build-provenance.json"
                if line.endswith("  build-provenance.json") else line
                for line in lines
            ]
            (source / "SHA256SUMS").write_text("\n".join(lines) + "\n")
            audit_lines = (source / "AUDIT-SHA256SUMS").read_text().splitlines()
            audit_lines = [
                f"{digest(data)}  build-provenance.json"
                if line.endswith("  build-provenance.json") else line
                for line in audit_lines
            ]
            (source / "AUDIT-SHA256SUMS").write_text("\n".join(audit_lines) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private/local material", result.stderr)

    def test_rejects_empty_pass_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            report = json.loads((source / "capture-source-gates.json").read_text())
            report["gates"] = []
            (source / "capture-source-gates.json").write_text(json.dumps(report) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("empty or invalid gates", result.stderr)

    def test_rejects_missing_expected_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            report = json.loads((source / "capture-source-gates.json").read_text())
            report["gates"].pop()
            (source / "capture-source-gates.json").write_text(json.dumps(report) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("gate set differs from lock", result.stderr)

    def test_rejects_duplicate_gate_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            report = json.loads((source / "capture-source-gates.json").read_text())
            report["gates"].append(dict(report["gates"][0]))
            (source / "capture-source-gates.json").write_text(json.dumps(report) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate gate names", result.stderr)

    def test_rejects_provenance_report_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["audit_report_sha256"]["source-pristine-gates.json"] = "0" * 64
            data = (json.dumps(provenance) + "\n").encode()
            (source / "build-provenance.json").write_bytes(data)
            for sums_name in ("SHA256SUMS", "AUDIT-SHA256SUMS"):
                lines = (source / sums_name).read_text().splitlines()
                lines = [
                    f"{digest(data)}  build-provenance.json"
                    if line.endswith("  build-provenance.json") else line
                    for line in lines
                ]
                (source / sums_name).write_text("\n".join(lines) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provenance report hash differs", result.stderr)

    def test_rejects_missing_ca_bootstrap_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            del provenance["builder"]["ca_bootstrap"]
            data = (json.dumps(provenance) + "\n").encode()
            (source / "build-provenance.json").write_bytes(data)
            for sums_name in ("SHA256SUMS", "AUDIT-SHA256SUMS"):
                lines = (source / sums_name).read_text().splitlines()
                lines = [
                    f"{digest(data)}  build-provenance.json"
                    if line.endswith("  build-provenance.json") else line
                    for line in lines
                ]
                (source / sums_name).write_text("\n".join(lines) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provenance shape differs", result.stderr)

    def test_rejects_wrong_capture_archive_toolchain_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["capture_source_archive_toolchain"]["zstd_threads"] = 0
            data = (json.dumps(provenance) + "\n").encode()
            (source / "build-provenance.json").write_bytes(data)
            for sums_name in ("SHA256SUMS", "AUDIT-SHA256SUMS"):
                lines = (source / sums_name).read_text().splitlines()
                lines = [
                    f"{digest(data)}  build-provenance.json"
                    if line.endswith("  build-provenance.json") else line
                    for line in lines
                ]
                (source / sums_name).write_text("\n".join(lines) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provenance source/image identity differs", result.stderr)

    def test_rejects_symlinked_release_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, bundle = self.fixture(base)
            real = base / "real-image.bin"
            real.write_bytes((source / IMAGE).read_bytes())
            (source / IMAGE).unlink()
            (source / IMAGE).symlink_to(real)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required build outputs are missing", result.stderr)

    def test_rejects_symlinked_bundle_directory_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source, _ = self.fixture(base)
            real_bundle = base / "real-bundle"
            real_bundle.mkdir()
            linked_bundle = base / "linked-bundle"
            linked_bundle.symlink_to(real_bundle, target_is_directory=True)
            result = self.run_bundle(source, linked_bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("arguments must not be symlinks", result.stderr)


if __name__ == "__main__":
    unittest.main()
