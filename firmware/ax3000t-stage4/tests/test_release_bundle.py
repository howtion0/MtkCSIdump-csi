#!/usr/bin/env python3
"""Hardware-free tests for the public GitHub Release allowlist."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare_release_bundle.py"
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_release_bundle as release_bundle  # noqa: E402
from prepare_release_bundle import (  # noqa: E402
    AUDIT_ASSETS,
    INTERNAL_PASS_REPORTS,
    FINAL_REQUIRED_ARTIFACTS,
    LOCKED_BASE_DIGEST,
    REPORT_SPECS,
    TOOLING_FILES,
    snapshot_build_outputs,
    write_release_bundle,
)
from download_closure import canonical_json_bytes  # noqa: E402

IMAGE = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseBundleTest(unittest.TestCase):
    def test_vanilla_mt76_serial_gate_is_independently_locked(self) -> None:
        gate_name = "builder.package_parallelism.vanilla_mt76"
        self.assertIn(gate_name, REPORT_SPECS["source-pristine-gates.json"]["gates"])
        self.assertIn(gate_name, REPORT_SPECS["source-patched-gates.json"]["gates"])
        verifier = (ROOT / "scripts/verify_sources.py").read_text()
        self.assertEqual(
            verifier.count(f'check(gates, "{gate_name}"'),
            1,
        )

    def test_canonical_vanilla_fingerprint_replaces_false_live_byte_gate(self) -> None:
        expected = "module.vanilla.canonical_builder_fingerprint"
        rejected = "module.baseline.byte_identity"
        for report_name in ("gate-report.json", "vanilla-abi-gates.json"):
            gates = REPORT_SPECS[report_name]["gates"]
            self.assertIn(expected, gates)
            self.assertNotIn(rejected, gates)

    def test_current_source_lock_digest_is_reused_from_the_tooling_snapshot(self) -> None:
        bundler = SCRIPT.read_text()
        self.assertIn(
            'current_source_lock_sha256 = actual_tooling_hashes["source-lock.json"]',
            bundler,
        )
        self.assertNotIn(
            'current_source_lock_sha256 = sha256(stage_root / "source-lock.json")',
            bundler,
        )

    def test_bundle_output_uses_exclusive_nofollow_directory_fd_writes(self) -> None:
        bundler = SCRIPT.read_text()
        for token in (
            'os.mkdir(bundle.name, 0o755, dir_fd=parent_fd)',
            'bundle_fd = os.open(bundle.name, directory_flags, dir_fd=parent_fd)',
            'os.O_WRONLY | os.O_CREAT | os.O_EXCL',
            'output_fd = os.open(name, write_flags, 0o644, dir_fd=bundle_fd)',
            'os.stat(name, dir_fd=bundle_fd, follow_symlinks=False)',
        ):
            self.assertIn(token, bundler)
        self.assertNotIn("shutil.copy2", bundler)

    @staticmethod
    def refresh_sum(source: Path, sums_name: str, name: str) -> None:
        lines = (source / sums_name).read_text().splitlines()
        replacement = f"{digest((source / name).read_bytes())}  {name}"
        matches = [index for index, line in enumerate(lines) if line.endswith(f"  {name}")]
        if len(matches) != 1:
            raise AssertionError(f"expected one {name} entry in {sums_name}")
        lines[matches[0]] = replacement
        (source / sums_name).write_text("\n".join(lines) + "\n")

    def rewrite_provenance(self, source: Path, provenance: dict) -> None:
        (source / "build-provenance.json").write_text(json.dumps(provenance) + "\n")
        self.refresh_sum(source, "SHA256SUMS", "build-provenance.json")
        self.refresh_sum(source, "AUDIT-SHA256SUMS", "build-provenance.json")

    def fixture(self, base: Path) -> tuple[Path, Path]:
        stage_root = base / "stage"
        shutil.copytree(ROOT, stage_root)
        self.script = stage_root / "scripts/prepare_release_bundle.py"
        source = base / "out"
        bundle = base / "bundle"
        source.mkdir()
        image = b"generic-source-built-test-image\n"
        (source / IMAGE).write_bytes(image)
        for name in AUDIT_ASSETS:
            path = source / name
            if not path.exists():
                path.write_bytes(("fixture:" + name + "\n").encode())
        closure = {
            "schema": 1,
            "directories": ["."],
            "files": [{"path": "fixture.tar.zst", "bytes": 7, "sha256": "9" * 64}],
        }
        closure_bytes = canonical_json_bytes(closure)
        (source / "download-closure.json").write_bytes(closure_bytes)
        public_key = (source / "ax3000t-stage4.pub").read_bytes()
        base_ucert = (source / "ax3000t-stage4.ucert").read_bytes()
        fingerprint = "0123456789abcdef"
        signing_lock = json.loads((stage_root / "source-lock.json").read_text())
        signing_lock["builder"]["download_closure"] = {
            "schema": 1,
            "directories": 1,
            "files": 1,
            "manifest_sha256": digest(closure_bytes),
        }
        signing_lock["signing"] = {
            "status": "READY",
            "public_key_sha256": digest(public_key),
            "base_ucert_sha256": digest(base_ucert),
            "usign_fingerprint": fingerprint,
            "base_ucert_validfrom": 1788126829,
            "base_ucert_expiresat": 1819662829,
        }
        vanilla_module = (source / "mt7915e.vanilla.ko").read_bytes()
        signing_lock["canonical_builder_vanilla"]["module_bytes"] = len(vanilla_module)
        signing_lock["canonical_builder_vanilla"]["module_sha256"] = digest(vanilla_module)
        source_lock_bytes = (
            json.dumps(signing_lock, indent=2, sort_keys=True) + "\n"
        ).encode()
        (stage_root / "source-lock.json").write_bytes(source_lock_bytes)
        (source / "source-lock.json").write_bytes(source_lock_bytes)
        locked_builder = signing_lock["builder"]
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
        gate["artifacts"]["mt7915e_vanilla_module"] = {
            "bytes": len(vanilla_module),
            "sha256": digest(vanilla_module),
        }
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
                report.update({
                    "source_lock": "source-lock.json",
                    "source_lock_sha256": digest(source_lock_bytes),
                })
            elif name == "capture-source-gates.json":
                report.update({
                    "archive": "capture.tar.gz", "bytes": 123, "expected_bytes": 123,
                    "sha256": "d" * 64, "expected_sha256": "d" * 64,
                })
            elif name == "vanilla-abi-gates.json":
                report["purpose"] = "vanilla-before-CSI ABI gate"
                report["artifacts"] = {
                    "mt7915e_vanilla_module": {
                        "bytes": len(vanilla_module),
                        "sha256": digest(vanilla_module),
                    }
                }
            elif name == "reproducibility-gates.json":
                report.update({
                    "clean_build_count": 2,
                    "image": IMAGE,
                    "image_sha256": digest(image),
                })
            (source / name).write_text(json.dumps(report) + "\n")
        report_names = ("gate-report.json",) + INTERNAL_PASS_REPORTS
        stage4_source_commit = "a" * 40
        stage4_source_tree = "b" * 40
        stage4_source_archive_sha256 = "c" * 64
        builder_image_id = "sha256:" + "b" * 64
        prepare_receipt = {
            "schema": 1,
            "phase": "networked-prepare-complete",
            "stage4_source_commit": stage4_source_commit,
            "stage4_source_tree": stage4_source_tree,
            "stage4_source_archive_sha256": stage4_source_archive_sha256,
            "builder_image_id": builder_image_id,
            "builder_base_digest": LOCKED_BASE_DIGEST,
            "openwrt_commit": signing_lock["openwrt"]["commit"],
            "kwrt_commit": signing_lock["kwrt_layout_source"]["commit"],
            "final_config_sha256": digest((source / "build.config").read_bytes()),
            "download_manifest_sha256": digest(closure_bytes),
        }
        (source / "network-prepare-receipt.json").write_text(
            json.dumps(prepare_receipt, sort_keys=True) + "\n"
        )
        provenance = json.loads((stage_root / "manifest.template.json").read_text())
        provenance.update({
            "gate_result": "pass",
            "publication_ready": True,
            "reproducibility_pending": False,
            "flash_authorized": False,
            "stage4_source_commit": stage4_source_commit,
            "stage4_source_tree": stage4_source_tree,
            "stage4_source_archive_sha256": stage4_source_archive_sha256,
            "image": {"name": IMAGE, "bytes": len(image), "sha256": digest(image)},
            "gate_report_sha256": digest((source / "gate-report.json").read_bytes()),
            "audit_report_sha256": {
                name: digest((source / name).read_bytes()) for name in report_names
            },
            "tooling_sha256": {
                name: digest((stage_root / name).read_bytes()) for name in TOOLING_FILES
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
            "vanilla_module": {
                "bytes": len(vanilla_module),
                "sha256": digest(vanilla_module),
            },
        })
        provenance["builder"].update({
            "base_digest": LOCKED_BASE_DIGEST,
            "image_id": builder_image_id,
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
            [sys.executable, str(getattr(self, "script", SCRIPT)), "--build-output", str(source),
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

    def test_release_inputs_are_snapshotted_once_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "out"
            source.mkdir()
            original = source / "artifact"
            original.write_bytes(b"first")
            owner, snapshot, identities = snapshot_build_outputs(source, {"artifact"})
            try:
                original.write_bytes(b"second")
                self.assertEqual((snapshot / "artifact").read_bytes(), b"first")
                self.assertEqual(identities["artifact"], {
                    "bytes": 5,
                    "sha256": digest(b"first"),
                })
            finally:
                owner.cleanup()

            original.unlink()
            target = source / "target"
            target.write_bytes(b"target")
            original.symlink_to(target.name)
            with self.assertRaisesRegex(ValueError, "safely open release input"):
                snapshot_build_outputs(source, {"artifact"})

    def test_secure_bundle_writer_refuses_preexisting_asset_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "snapshot"
            bundle = base / "bundle"
            source.mkdir()
            bundle.mkdir()
            (source / "asset").write_bytes(b"public")
            outside = base / "outside"
            outside.write_bytes(b"keep")
            (bundle / "asset").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "new or empty"):
                write_release_bundle(
                    source,
                    bundle,
                    ("asset",),
                    {"asset": {"bytes": 6, "sha256": digest(b"public")}},
                )
            self.assertEqual(outside.read_bytes(), b"keep")

    def test_secure_bundle_writer_reads_back_and_rejects_corrupt_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "snapshot"
            bundle = base / "bundle"
            source.mkdir()
            (source / "asset").write_bytes(b"public")
            original_write = release_bundle.os.write

            def corrupt_write(descriptor, data):
                return original_write(descriptor, b"x" * len(data))

            with mock.patch.object(release_bundle.os, "write", side_effect=corrupt_write):
                with self.assertRaisesRegex(ValueError, "read-back hash differs"):
                    write_release_bundle(
                        source,
                        bundle,
                        ("asset",),
                        {"asset": {"bytes": 6, "sha256": digest(b"public")}},
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

    def test_rejects_boolean_report_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            gate = json.loads((source / "gate-report.json").read_text())
            gate["schema"] = True
            (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema is not exactly 1", result.stderr)

    def test_rejects_boolean_builder_parallelism_even_when_rehashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["jobs"] = True
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("build parallelism is outside", result.stderr)

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

    def test_rejects_synchronized_vanilla_module_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            module = source / "mt7915e.vanilla.ko"
            module.write_bytes(module.read_bytes() + b"tampered")
            identity = {"bytes": module.stat().st_size, "sha256": digest(module.read_bytes())}

            gate = json.loads((source / "gate-report.json").read_text())
            gate["artifacts"]["mt7915e_vanilla_module"] = identity
            (source / "gate-report.json").write_text(json.dumps(gate) + "\n")
            vanilla_name = "vanilla-abi-gates.json"
            vanilla_report = json.loads((source / vanilla_name).read_text())
            vanilla_report["artifacts"]["mt7915e_vanilla_module"] = identity
            (source / vanilla_name).write_text(json.dumps(vanilla_report) + "\n")

            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["vanilla_module"] = identity
            provenance["gate_report_sha256"] = digest(
                (source / "gate-report.json").read_bytes()
            )
            for name in ("gate-report.json", vanilla_name):
                provenance["audit_report_sha256"][name] = digest((source / name).read_bytes())
                self.refresh_sum(source, "AUDIT-SHA256SUMS", name)
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "mt7915e.vanilla.ko")
            self.refresh_sum(source, "SHA256SUMS", "gate-report.json")
            self.rewrite_provenance(source, provenance)

            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("canonical builder vanilla module differs", result.stderr)

    def test_rejects_synchronized_download_manifest_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            closure = json.loads((source / "download-closure.json").read_text())
            closure["files"][0]["bytes"] = 8
            (source / "download-closure.json").write_bytes(canonical_json_bytes(closure))
            manifest_sha = digest((source / "download-closure.json").read_bytes())
            receipt = json.loads((source / "network-prepare-receipt.json").read_text())
            receipt["download_manifest_sha256"] = manifest_sha
            (source / "network-prepare-receipt.json").write_text(json.dumps(receipt) + "\n")
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["download_manifest_sha256"] = manifest_sha
            provenance["builder"]["networked_prepare_receipt_sha256"] = digest(
                (source / "network-prepare-receipt.json").read_bytes()
            )
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "download-closure.json")
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "network-prepare-receipt.json")
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("download manifest identity differs", result.stderr)

    def test_rejects_provenance_download_manifest_edge_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["download_manifest_sha256"] = "0" * 64
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("download manifest is not cross-bound", result.stderr)

    def test_rejects_synchronized_prepare_receipt_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            receipt = json.loads((source / "network-prepare-receipt.json").read_text())
            receipt["openwrt_commit"] = "0" * 40
            (source / "network-prepare-receipt.json").write_text(json.dumps(receipt) + "\n")
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["networked_prepare_receipt_sha256"] = digest(
                (source / "network-prepare-receipt.json").read_bytes()
            )
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "network-prepare-receipt.json")
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("prepare receipt identity differs", result.stderr)

    def test_rejects_provenance_prepare_receipt_edge_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["networked_prepare_receipt_sha256"] = "0" * 64
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("networked prepare receipt is not cross-bound", result.stderr)

    def test_rejects_boolean_prepare_receipt_schema_even_when_rehashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            receipt = json.loads((source / "network-prepare-receipt.json").read_text())
            receipt["schema"] = True
            (source / "network-prepare-receipt.json").write_text(json.dumps(receipt) + "\n")
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["builder"]["networked_prepare_receipt_sha256"] = digest(
                (source / "network-prepare-receipt.json").read_bytes()
            )
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "network-prepare-receipt.json")
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("receipt schema is not an integer", result.stderr)

    def test_rejects_synchronized_output_source_lock_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            source_lock = json.loads((source / "source-lock.json").read_text())
            (source / "source-lock.json").write_text(json.dumps(source_lock) + "\n")
            source_lock_sha = digest((source / "source-lock.json").read_bytes())
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["source_lock_sha256"] = source_lock_sha
            for name in ("source-pristine-gates.json", "source-patched-gates.json"):
                report = json.loads((source / name).read_text())
                report["source_lock_sha256"] = source_lock_sha
                (source / name).write_text(json.dumps(report) + "\n")
                provenance["audit_report_sha256"][name] = digest((source / name).read_bytes())
                self.refresh_sum(source, "AUDIT-SHA256SUMS", name)
            self.refresh_sum(source, "AUDIT-SHA256SUMS", "source-lock.json")
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("output source lock differs", result.stderr)

    def test_rejects_source_report_bound_to_another_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, bundle = self.fixture(Path(tmp))
            name = "source-pristine-gates.json"
            report = json.loads((source / name).read_text())
            report["source_lock_sha256"] = "0" * 64
            (source / name).write_text(json.dumps(report) + "\n")
            provenance = json.loads((source / "build-provenance.json").read_text())
            provenance["audit_report_sha256"][name] = digest((source / name).read_bytes())
            self.refresh_sum(source, "AUDIT-SHA256SUMS", name)
            self.rewrite_provenance(source, provenance)
            result = self.run_bundle(source, bundle)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not bound to the exact output source lock", result.stderr)

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
