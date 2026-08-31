#!/usr/bin/env python3
"""Create a four-file, public-only GitHub Release bundle after every hard gate.

The source directory is never modified.  The destination must be new or empty,
and only an explicit filename allowlist is copied.  This makes it impossible
for a device dump sitting elsewhere on the workstation to enter the release by
way of a wildcard.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from compare_repro_builds import COMPARE_FILES as REPRO_COMPARE_FILES
from download_closure import load_download_lock, load_manifest_with_bytes


IMAGE = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"
RELEASE_ASSETS = (IMAGE, "SHA256SUMS", "build-provenance.json", "gate-report.json")
HASHED_ASSETS = (IMAGE, "gate-report.json", "build-provenance.json")
INTERNAL_PASS_REPORTS = (
    "source-pristine-gates.json",
    "source-patched-gates.json",
    "capture-source-gates.json",
    "vanilla-abi-gates.json",
    "reproducibility-gates.json",
)
TOOLING_FILES = (
    ".gitignore", "README.md", "BUILD.md", "PUBLIC-VS-PRIVATE.md", "RECOVERY.md", "RELEASE.md",
    "build-config.seed", "manifest.template.json", "source-lock.json", "verify_image.py",
    "container/Dockerfile", "container/apt-sources.list",
    "scripts/build_image.sh", "scripts/run_container_build.sh",
    "scripts/prepare_release_bundle.py", "scripts/verify_sources.py",
    "scripts/verify_vanilla_abi.py", "scripts/verify_capture_archive.py",
    "scripts/download_closure.py", "scripts/preflight_single_ubi.sh",
    "scripts/compare_repro_builds.py", "scripts/run_repro_pair.sh",
    "package/mtkcsi-dump/Makefile", "package/mtkcsi-dump/files/mtkcsi.config",
    "package/mtkcsi-dump/files/mtkcsi-dump.init",
    "patches/10-kwrt-vermagic-one.patch", "patches/23-ax3000t.patch",
    "patches/25-platform.patch", "patches/26-ax3000t-single-ubi-compat.patch",
    "patches/999-mt7915-csi-v2-hardened.patch",
    "tests/test_builder_lock.py", "tests/test_capture_gate.py", "tests/test_download_closure.py",
    "tests/test_fit_gate.py", "tests/test_metadata_gate.py",
    "keys/README.md", "tests/test_privacy_gate.py",
    "tests/test_release_bundle.py", "tests/test_repro_pair.py", "tests/test_tar_closure.py",
    "tests/test_wifi_gate.py",
)
AUDIT_ASSETS = (
    IMAGE, "packages.manifest", "mt7915e.ko", "kmod-mt7915e.ipk",
    "mt7915e.vanilla.ko", "kmod-mt7915e.vanilla.ipk", "kernel.release", "kernel.config",
    "Module.symvers", "platform.sh", "build.config", "kwrt-exact.config", "source-lock.json",
    "source-pristine-gates.json",
    "source-patched-gates.json", "vanilla-abi-gates.json", "capture-source-gates.json",
    "gate-report.json", "build-provenance.json", "build.log", "builder-packages.txt",
    "network-prepare-receipt.json", "download-closure.json", "reproducibility-gates.json",
    "ax3000t-stage4.pub", "ax3000t-stage4.ucert",
)
LOCKED_BASE_DIGEST = (
    "ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982"
)
FORBIDDEN_FILENAME = re.compile(
    r"(?i)(?:^|[-_.])(?:mtd\d*|factory|nvram|bdata|eeprom|calibration|"
    r"backup|dump|ubi(?:dump|backup)?)(?:[-_.]|$)"
)
FORBIDDEN_PUBLIC_TEXT = (
    b"/Users/",
    b"/home/",
    b"C:\\Users\\",
    b"ax3000t-backup",
    b"runtime-files/",
    b"rollback-packages/",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)

FINAL_GATE_NAMES = frozenset({
    "sysupgrade.tar.closure", "sysupgrade.tar.root", "sysupgrade.tar.members",
    "sysupgrade.control.board", "ubi.payload.capacity",
    "metadata.fwtool.present", "metadata.fwtool.unique_info", "metadata.fwtool.header",
    "metadata.schema", "metadata.metadata_version", "metadata.supported_devices",
    "metadata.legacy_supported_message", "metadata.compat_version", "metadata.compat_message",
    "metadata.version.schema", "metadata.version.identity", "metadata.target", "metadata.board",
    "fit.payload.hashes", "fit.configuration.selection", "fit.kernel.identity",
    "fit.kernel.selected_hash", "fit.kernel.decompress", "fit.kernel.arm64_header",
    "fit.kernel.release_string", "fit.dtb.selection", "fit.dtb.selected_hash",
    "dtb.compatible", "dtb.fixed_partitions.path", "dtb.spi_nand.identity",
    "dtb.fixed_partitions.cells", "dtb.fixed_partitions.order", "dtb.fixed_partitions.exact",
    "dtb.single_ubi_112m", "dtb.kf.boundary", "dtb.no_ubi_kernel",
    "dtb.no_stock_root_ubi", "rootfs.extract",
    "privacy.private_key_paths", "privacy.sensitive_symlinks", "privacy.read_complete",
    "privacy.credentials", "privacy.device_macs",
    "rootfs.module.present", "rootfs.module.identity", "rootfs.capture.binary",
    "rootfs.capture.radio_epoch_gates", "rootfs.capture.docs",
    "rootfs.capture.channel_semantics_docs", "rootfs.capture.disabled",
    "rootfs.wifi.generator", "rootfs.wifi.safe_default", "rootfs.packages.no_remote_feed",
    "rootfs.packages.signature_check", "rootfs.release.neutral_branding",
    "rootfs.release.no_kwrt_branding", "signature.inputs", "signature.public_inputs",
    "signature.base_ucert_identity", "signature.fingerprint",
    "signature.base_ucert_prefix", "signature.crypto",
    "signature.rootfs_trust",
    "upgrade.platform.final", "upgrade.platform.exact_source", "upgrade.platform.function",
    "upgrade.generic_nand.path", "upgrade.generic_nand.default",
    "upgrade.no_ubi_kernel_override", "upgrade.no_layout_conversion",
    "upgrade.platform.provenance", "packages.kernel.release", "packages.mt76.revision",
    "packages.mt76.dependencies", "packages.capture.pinned",
    "module.patched.architecture", "module.patched.this_module_size",
    "module.patched.vermagic", "module.patched.dependencies", "module.patched.no_modversions",
    "module.patched.undefined_symbols", "module.patched.csi_present",
    "module.baseline.architecture", "module.baseline.this_module_size",
    "module.baseline.vermagic", "module.baseline.dependencies", "module.baseline.no_modversions",
    "module.vanilla.canonical_builder_fingerprint", "module.baseline.undefined_symbols",
    "module.baseline.no_csi", "module.patched.exact_symbol_delta",
    "kmod.patched.kernel_dependency", "kmod.patched.identity", "kmod.patched.module_identity",
    "kmod.baseline.kernel_dependency", "kmod.baseline.identity", "kmod.baseline.module_identity",
    "kernel.release.file", "kernel.config.abi_flags",
    "kernel.module_symvers.csi_dependencies",
})

SOURCE_COMMON_GATE_NAMES = frozenset({
    "build.overlay.sha256",
    "build.overlay.safe_packages.CONFIG_SIGNED_PACKAGES",
    "build.overlay.safe_packages.CONFIG_SIGNATURE_CHECK",
    "build.overlay.safe_packages.CONFIG_PER_FEED_REPO",
    "build.overlay.safe_packages.CONFIG_KERNEL_BUILD_USER",
    "build.overlay.safe_packages.CONFIG_KERNEL_BUILD_DOMAIN",
    "build.overlay.safe_packages.CONFIG_VERSION_DIST",
    "build.overlay.safe_packages.CONFIG_VERSION_NUMBER",
    "build.overlay.safe_packages.CONFIG_VERSION_CODE",
    "build.overlay.safe_packages.CONFIG_VERSION_REPO",
    "build.overlay.safe_packages.CONFIG_VERSION_MANUFACTURER",
    "build.overlay.safe_packages.CONFIG_VERSION_HOME_URL",
    "build.overlay.safe_packages.CONFIG_VERSION_MANUFACTURER_URL",
    "build.overlay.no_third_party_repo", "builder.dockerfile.sha256", "builder.base_digest",
    "builder.source_date_epoch", "builder.apt_sources.sha256", "builder.apt_snapshot",
    "builder.apt_snapshot.direct_uri", "builder.apt_archive_keyring.lock",
    "builder.ca_bootstrap.package_lock", "builder.ca_bootstrap.bundle_lock",
    "builder.ca_bootstrap.tls_enforced",
    "builder.apt_sources.copy",
    "builder.download_closure.prepare_targets", "builder.offline.prepare_order",
    "builder.prepare_dependency_closure", "builder.download_closure.identity",
    "builder.package_parallelism.lua", "builder.package_parallelism.vanilla_mt76",
    "signing_tools.usign.commit", "signing_tools.usign.mirror_hash",
    "signing_tools.ucert.commit", "signing_tools.ucert.mirror_hash",
    "signing.status.ready", "signing.public_key.sha256", "signing.base_ucert.sha256",
    "signing.fingerprint.locked", "signing.base_ucert.validity_lock",
    "signing.private_key.absent",
    "capture.package.exact_tree", "capture.package.sha256.Makefile",
    "capture.package.sha256.files/mtkcsi.config",
    "capture.package.sha256.files/mtkcsi-dump.init", "capture.source.commit",
    "capture.source.archive_hash", "capture.source.git_protocol",
    "capture.source.canonical_lock", "capture.source.archive_builder_lock",
    "openwrt.commit", "openwrt.pristine.tree",
    "openwrt.revision.release_code",
    "kwrt.commit", "kwrt.tree", "kwrt.worktree.clean",
    "kwrt.config.common.sha256", "kwrt.config.target.sha256",
    "kwrt.config.ipk_mode", "kwrt.common_diy.sha256",
    "kwrt.common_diy.vermagic_source_line", "kwrt.patch.origin.23-ax3000t.patch",
    "kwrt.patch.origin.25-platform.patch", "openwrt.mt76.commit",
    "openwrt.mt76.source_date", "openwrt.mt76.package_release", "wifi.generator.sha256",
    "wifi.generator.default_disabled_expression", "wifi.generator.default_empty_key_expression",
} | {f"patch.sha256.{name}" for name in (
    "10-kwrt-vermagic-one.patch", "23-ax3000t.patch", "25-platform.patch",
    "26-ax3000t-single-ubi-compat.patch", "999-mt7915-csi-v2-hardened.patch",
)} | {f"patch.normalization.25-platform.patch.{suffix}" for suffix in (
    "historical_missing_lf", "one_lf_only", "historical_hunk_count",
    "application_stream_sha256",
)})
SOURCE_PRISTINE_GATE_NAMES = SOURCE_COMMON_GATE_NAMES | {
    "openwrt.worktree.pristine",
    "kwrt.vermagic.pristine", "layout.pristine.stock", "upgrade.pristine.special",
    "metadata.compat.pristine_stock",
}
SOURCE_PATCHED_GATE_NAMES = SOURCE_COMMON_GATE_NAMES | {
    "openwrt.worktree.patched_exact",
    "kwrt.vermagic.patched", "layout.patched.single_ubi", "upgrade.patched.generic",
    "metadata.compat.patched", "openwrt.mt76.hardened_patch.installed",
} | {f"post_patch_file.sha256.{name}" for name in (
    "include/kernel-defaults.mk",
    "target/linux/mediatek/dts/mt7981b-xiaomi-mi-router-ax3000t.dts",
    "target/linux/mediatek/image/filogic.mk",
    "target/linux/mediatek/filogic/base-files/lib/upgrade/platform.sh",
)}
CAPTURE_GATE_NAMES = frozenset({
    "capture.source_lock.identity", "capture.package.git_source",
    "capture.archive.bytes", "capture.archive.sha256",
    "capture.toolchain.git", "capture.toolchain.gnu_tar", "capture.toolchain.zstd",
    "capture.archive.regular_members", "capture.git.tree",
    "capture.archive.member_closure", "capture.git.commit",
})
VANILLA_GATE_NAMES = frozenset({
    "module.baseline.architecture", "module.baseline.this_module_size",
    "module.baseline.vermagic", "module.baseline.dependencies", "module.baseline.no_modversions",
    "module.vanilla.canonical_builder_fingerprint", "module.baseline.undefined_symbols",
    "module.baseline.no_csi", "kmod.baseline.kernel_dependency", "kmod.baseline.identity",
    "kmod.baseline.module_identity",
})
REPRO_GATE_NAMES = frozenset(
    {f"repro.byte_identity.{name}" for name in REPRO_COMPARE_FILES} |
    {"repro.provenance.build_identity"}
)
REPORT_SPECS = {
    "gate-report.json": {
        "classification": "EXPERIMENTAL-DO-NOT-FLASH", "gates": FINAL_GATE_NAMES,
        "gate_fields": {"name", "status", "detail", "evidence"},
        "top_fields": {
            "schema", "classification", "flash_authorized", "image", "result",
            "artifacts", "gates",
        },
    },
    "source-pristine-gates.json": {
        "classification": "EXPERIMENTAL-DO-NOT-FLASH", "phase": "pristine",
        "gates": SOURCE_PRISTINE_GATE_NAMES,
        "gate_fields": {"name", "status", "expected", "actual"},
    },
    "source-patched-gates.json": {
        "classification": "EXPERIMENTAL-DO-NOT-FLASH", "phase": "patched",
        "gates": SOURCE_PATCHED_GATE_NAMES,
        "gate_fields": {"name", "status", "expected", "actual"},
    },
    "capture-source-gates.json": {
        "classification": "public-build-input", "gates": CAPTURE_GATE_NAMES,
        "gate_fields": {"name", "status", "expected", "actual"},
    },
    "vanilla-abi-gates.json": {
        "classification": "EXPERIMENTAL-DO-NOT-FLASH", "gates": VANILLA_GATE_NAMES,
        "gate_fields": {"name", "status", "detail", "evidence"},
    },
    "reproducibility-gates.json": {
        "classification": "EXPERIMENTAL-DO-NOT-FLASH", "gates": REPRO_GATE_NAMES,
        "gate_fields": {"name", "status", "detail", "evidence"},
    },
}
FINAL_REQUIRED_ARTIFACTS = frozenset({
    "image", "CONTROL", "kernel", "root", "fwtool_chunks", "metadata",
    "fit_kernel", "embedded_dtb", "privacy_scan", "signature",
    "mt7915e_module", "mt7915e_vanilla_module", "kmod_ipk",
    "vanilla_kmod_ipk", "capture_binary", "package_manifest", "platform_sh",
    "kernel_release_file", "kernel_config", "module_symvers",
})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def snapshot_build_outputs(source: Path, names: set[str]) -> tuple[Any, Path, dict[str, dict[str, Any]]]:
    """Copy all build outputs once through a stable no-follow directory handle.

    Every later semantic/hash/copy check uses this private snapshot, never a
    pathname that another local process can swap between validation steps.
    """
    owner = tempfile.TemporaryDirectory(prefix="ax3000t-stage4-release-inputs-")
    snapshot = Path(owner.name)
    directory_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                       getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        directory_fd = os.open(source, directory_flags)
    except OSError as exc:
        owner.cleanup()
        raise ValueError("cannot safely open the build-output directory") from exc
    try:
        directory_before = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise ValueError("build output is not a stable directory")
        identities: dict[str, dict[str, Any]] = {}
        for name in sorted(names, key=os.fsencode):
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError(f"unsafe release input name: {name!r}")
            source_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                            getattr(os, "O_NOFOLLOW", 0))
            try:
                source_fd = os.open(name, source_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise ValueError(f"cannot safely open release input: {name}") from exc
            destination = snapshot / name
            destination_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_CLOEXEC", 0) |
                                 getattr(os, "O_NOFOLLOW", 0))
            destination_fd = -1
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"release input is not a regular file: {name}")
                destination_fd = os.open(destination, destination_flags, 0o600)
                value = hashlib.sha256()
                byte_count = 0
                while True:
                    block = os.read(source_fd, 1024 * 1024)
                    if not block:
                        break
                    value.update(block)
                    byte_count += len(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise OSError("short write while snapshotting release input")
                        view = view[written:]
                os.fsync(destination_fd)
                after = os.fstat(source_fd)
                stable_fields = (
                    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
                )
                if any(getattr(before, field) != getattr(after, field)
                       for field in stable_fields):
                    raise ValueError(f"release input changed while snapshotting: {name}")
                if byte_count != before.st_size:
                    raise ValueError(f"release input size changed while snapshotting: {name}")
                identities[name] = {
                    "bytes": byte_count,
                    "sha256": value.hexdigest(),
                }
            finally:
                os.close(source_fd)
                if destination_fd >= 0:
                    os.close(destination_fd)
        directory_after = os.fstat(directory_fd)
        stable_directory_fields = (
            "st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(directory_before, field) != getattr(directory_after, field)
               for field in stable_directory_fields):
            raise ValueError("build-output directory changed while inputs were snapshotted")
    except Exception:
        owner.cleanup()
        raise
    finally:
        os.close(directory_fd)
    return owner, snapshot, identities


def write_release_bundle(source: Path, bundle: Path, names: tuple[str, ...],
                         expected: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Write the exact bundle through stable directory FDs without following links."""
    if Path(bundle.name).name != bundle.name or bundle.name in {".", ".."}:
        raise ValueError("unsafe release bundle directory name")
    if set(expected) != set(names):
        raise ValueError("release asset identity cache differs from the exact allowlist")
    directory_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                       getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        parent_fd = os.open(bundle.parent, directory_flags)
    except OSError as exc:
        raise ValueError("release bundle parent must be an existing regular directory") from exc
    bundle_fd = -1
    source_fd = -1
    try:
        try:
            os.mkdir(bundle.name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            bundle_fd = os.open(bundle.name, directory_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("release bundle path is not a safe directory") from exc
        opened_bundle = os.fstat(bundle_fd)
        bundle_entry = os.stat(bundle.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(opened_bundle.st_mode) or
                opened_bundle.st_dev != bundle_entry.st_dev or
                opened_bundle.st_ino != bundle_entry.st_ino):
            raise ValueError("release bundle directory changed while it was opened")
        with os.scandir(bundle_fd) as iterator:
            existing = [entry.name for entry in iterator]
        if existing:
            raise ValueError("bundle directory must be new or empty")
        parent_stable = os.fstat(parent_fd)

        source_fd = os.open(source, directory_flags)
        copied: dict[str, dict[str, Any]] = {}
        for name in names:
            if Path(name).name != name or name in {".", ".."}:
                raise ValueError(f"unsafe release asset name: {name!r}")
            read_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                          getattr(os, "O_NOFOLLOW", 0))
            write_flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL |
                           getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                input_fd = os.open(name, read_flags, dir_fd=source_fd)
            except OSError as exc:
                raise ValueError(f"cannot safely reopen snapshotted asset: {name}") from exc
            output_fd = -1
            value = hashlib.sha256()
            byte_count = 0
            try:
                input_before = os.fstat(input_fd)
                if not stat.S_ISREG(input_before.st_mode):
                    raise ValueError(f"snapshotted release asset is not regular: {name}")
                try:
                    output_fd = os.open(name, write_flags, 0o644, dir_fd=bundle_fd)
                except OSError as exc:
                    raise ValueError(f"refusing unsafe or pre-existing release asset: {name}") from exc
                while True:
                    block = os.read(input_fd, 1024 * 1024)
                    if not block:
                        break
                    value.update(block)
                    byte_count += len(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(output_fd, view)
                        if written <= 0:
                            raise OSError("short write while creating release asset")
                        view = view[written:]
                os.fsync(output_fd)
                input_after = os.fstat(input_fd)
                stable_file_fields = (
                    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
                )
                if any(getattr(input_before, field) != getattr(input_after, field)
                       for field in stable_file_fields):
                    raise ValueError(f"snapshotted release asset changed while copying: {name}")
                input_digest = value.hexdigest()
                if expected[name] != {"bytes": byte_count, "sha256": input_digest}:
                    raise ValueError(f"snapshotted release asset differs from initial input: {name}")
                output_before_read = os.fstat(output_fd)
                if (not stat.S_ISREG(output_before_read.st_mode) or
                        output_before_read.st_size != byte_count):
                    raise ValueError(f"copied release asset identity is invalid: {name}")
                os.lseek(output_fd, 0, os.SEEK_SET)
                output_value = hashlib.sha256()
                output_bytes = 0
                while True:
                    block = os.read(output_fd, 1024 * 1024)
                    if not block:
                        break
                    output_value.update(block)
                    output_bytes += len(block)
                output_identity = os.fstat(output_fd)
                if any(getattr(output_before_read, field) != getattr(output_identity, field)
                       for field in stable_file_fields):
                    raise ValueError(f"release asset changed while reading it back: {name}")
                if (output_bytes != byte_count or output_value.hexdigest() != input_digest):
                    raise ValueError(f"release asset read-back hash differs after copying: {name}")
                copied[name] = {
                    "name": name,
                    "bytes": byte_count,
                    "sha256": input_digest,
                    "identity": tuple(
                        getattr(output_identity, field) for field in stable_file_fields
                    ),
                }
            finally:
                os.close(input_fd)
                if output_fd >= 0:
                    os.close(output_fd)

        os.fsync(bundle_fd)
        bundle_stable = os.fstat(bundle_fd)
        with os.scandir(bundle_fd) as iterator:
            final_names = sorted((entry.name for entry in iterator), key=os.fsencode)
        if final_names != sorted(names, key=os.fsencode):
            raise ValueError("copied release closure differs from the exact four assets")
        stable_file_fields = (
            "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns",
        )
        for name in names:
            final_identity = os.stat(name, dir_fd=bundle_fd, follow_symlinks=False)
            if (not stat.S_ISREG(final_identity.st_mode) or
                    tuple(getattr(final_identity, field) for field in stable_file_fields) !=
                    copied[name]["identity"]):
                raise ValueError(f"release asset changed after copying: {name}")
        bundle_after = os.fstat(bundle_fd)
        bundle_entry_after = os.stat(bundle.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        stable_directory_fields = (
            "st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(bundle_stable, field) != getattr(bundle_after, field)
               for field in stable_directory_fields):
            raise ValueError("release bundle directory changed during final verification")
        if (bundle_entry_after.st_dev != opened_bundle.st_dev or
                bundle_entry_after.st_ino != opened_bundle.st_ino or
                any(getattr(parent_stable, field) != getattr(parent_after, field)
                    for field in stable_directory_fields)):
            raise ValueError("release bundle path changed while assets were written")
        return [
            {key: copied[name][key] for key in ("name", "bytes", "sha256")}
            for name in names
        ]
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if bundle_fd >= 0:
            os.close(bundle_fd)
        os.close(parent_fd)


def load_json(path: Path) -> dict[str, Any]:
    if not is_regular_file(path):
        raise ValueError(f"{path.name} is missing, non-regular, or a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return value


def json_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return "list"
    return "scalar"


def provenance_shape(value: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical structural schema while normalizing locked hash maps.

    The two maps have a statically enforced key set later in the verifier.  Their
    template values are intentionally empty because materializing every tooling
    and report filename twice would create a second schema authority.
    """
    normalized = dict(value)
    for key in ("audit_report_sha256", "tooling_sha256"):
        if not isinstance(normalized.get(key), dict):
            return {"__invalid_dynamic_map__": key}
        normalized[key] = {}
    return json_shape(normalized)


def parse_sums(path: Path, expected_names: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    if not is_regular_file(path):
        raise ValueError(f"{path.name} is missing, non-regular, or a symlink")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read SHA256SUMS: {exc}") from exc
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._~-]*)", line)
        if not match:
            raise ValueError(f"invalid SHA256SUMS line: {line!r}")
        digest, name = match.groups()
        if name in result:
            raise ValueError(f"duplicate SHA256SUMS entry: {name}")
        result[name] = digest
    if set(result) != set(expected_names):
        raise ValueError(
            f"{path.name} must name exactly {list(expected_names)}, got {sorted(result)}"
        )
    return result


def require_pass_report(path: Path) -> dict[str, Any]:
    report = load_json(path)
    spec = REPORT_SPECS.get(path.name)
    if spec is None:
        raise ValueError(f"no locked report schema exists for {path.name}")
    if type(report.get("schema")) is not int or report["schema"] != 1:
        raise ValueError(f"{path.name} schema is not exactly 1")
    if report.get("classification") != spec["classification"]:
        raise ValueError(f"{path.name} classification differs from the lock")
    if "top_fields" in spec and set(report) != spec["top_fields"]:
        raise ValueError(f"{path.name} top-level schema differs from the lock")
    if path.name == "gate-report.json" and report.get("flash_authorized") is not False:
        raise ValueError("gate-report.json must explicitly deny flash authorization")
    if "phase" in spec and report.get("phase") != spec["phase"]:
        raise ValueError(f"{path.name} phase differs from the lock")
    if path.name.startswith("source-"):
        if (report.get("source_lock") != "source-lock.json" or
                re.fullmatch(r"[0-9a-f]{64}", str(report.get("source_lock_sha256", ""))) is None):
            raise ValueError(f"{path.name} lacks exact source-lock identity evidence")
    elif path.name == "capture-source-gates.json":
        if (not isinstance(report.get("bytes"), int) or report.get("bytes", 0) <= 0 or
                re.fullmatch(r"[0-9a-f]{64}", str(report.get("sha256", ""))) is None or
                report.get("bytes") != report.get("expected_bytes") or
                report.get("sha256") != report.get("expected_sha256")):
            raise ValueError("capture-source-gates.json lacks exact archive byte/hash evidence")
    elif path.name == "vanilla-abi-gates.json":
        if report.get("purpose") != "vanilla-before-CSI ABI gate":
            raise ValueError("vanilla ABI report purpose differs from the lock")
    elif path.name == "reproducibility-gates.json":
        if (report.get("clean_build_count") != 2 or report.get("image") != IMAGE or
                re.fullmatch(r"[0-9a-f]{64}", str(report.get("image_sha256", ""))) is None):
            raise ValueError("reproducibility report lacks exact two-build image evidence")
    if report.get("result") != "pass":
        raise ValueError(f"{path.name} is not a passing report")
    gates = report.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ValueError(f"{path.name} has an empty or invalid gates list")
    failures = []
    names: list[str] = []
    for gate in gates:
        if not isinstance(gate, dict):
            failures.append("<non-object>")
            continue
        name = gate.get("name")
        if (not isinstance(name, str) or not name or gate.get("status") != "pass" or
                set(gate) != spec["gate_fields"]):
            failures.append(str(name or "<unnamed>"))
        if isinstance(name, str):
            names.append(name)
    if failures:
        raise ValueError(f"{path.name} contains non-pass gates: {failures}")
    if len(names) != len(set(names)):
        raise ValueError(f"{path.name} contains duplicate gate names")
    if set(names) != spec["gates"]:
        missing = sorted(spec["gates"] - set(names))
        extra = sorted(set(names) - spec["gates"])
        raise ValueError(
            f"{path.name} gate set differs from lock; missing={missing}, extra={extra}"
        )
    return report


def reject_private_markers(source: Path) -> None:
    """Scan the complete public allowlist before interpreting its contents.

    Keeping this check ahead of semantic provenance validation makes a local
    path or private-key marker a first-class release blocker even when the same
    tampering would also invalidate a later cross-binding.
    """
    for name in RELEASE_ASSETS:
        if FORBIDDEN_FILENAME.search(name):
            raise ValueError(f"forbidden private-artifact filename: {name}")
        data = (source / name).read_bytes()
        hits = [needle.decode("ascii", "replace") for needle in FORBIDDEN_PUBLIC_TEXT
                if needle in data]
        if hits:
            raise ValueError(f"{name} contains private/local material markers: {hits}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-output", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    if args.build_output.is_symlink() or args.bundle.is_symlink():
        print("release bundle rejected: source and destination arguments must not be symlinks",
              file=sys.stderr)
        return 1
    source = args.build_output.resolve()
    bundle = args.bundle.resolve()
    snapshot_owner = None

    try:
        if not source.is_dir():
            raise ValueError("build output is not a directory")
        if source == bundle:
            raise ValueError("bundle directory must differ from build output")
        if bundle.exists() and (bundle.is_symlink() or not bundle.is_dir() or any(bundle.iterdir())):
            raise ValueError("bundle directory must be new or empty")

        required = set(RELEASE_ASSETS) | set(INTERNAL_PASS_REPORTS) | {
            "AUDIT-SHA256SUMS", "builder-packages.txt",
        } | set(AUDIT_ASSETS)
        missing = sorted(name for name in required if not is_regular_file(source / name))
        if missing:
            raise ValueError(f"required build outputs are missing: {missing}")
        snapshot_owner, source, snapshot_identities = snapshot_build_outputs(source, required)

        reject_private_markers(source)

        gate = require_pass_report(source / "gate-report.json")
        gate_artifacts = gate.get("artifacts")
        if (not isinstance(gate_artifacts, dict) or
                not FINAL_REQUIRED_ARTIFACTS <= set(gate_artifacts)):
            missing_artifacts = sorted(
                FINAL_REQUIRED_ARTIFACTS - set(gate_artifacts or {})
            )
            raise ValueError(f"gate-report required artifact evidence is missing: {missing_artifacts}")
        reports = {"gate-report.json": gate}
        for name in INTERNAL_PASS_REPORTS:
            reports[name] = require_pass_report(source / name)

        provenance = load_json(source / "build-provenance.json")
        if provenance.get("schema") != 2:
            raise ValueError("provenance schema is not the cross-bound Stage4 schema 2")
        if provenance.get("classification") != "EXPERIMENTAL-DO-NOT-FLASH":
            raise ValueError("provenance lacks EXPERIMENTAL-DO-NOT-FLASH classification")
        if provenance.get("gate_result") != "pass":
            raise ValueError("provenance does not record a passing final gate")
        if provenance.get("publication_ready") is not True:
            raise ValueError("provenance does not authorize public artifact publication")
        if provenance.get("flash_authorized") is not False:
            raise ValueError("experimental provenance must explicitly forbid flashing")
        if not re.fullmatch(r"[0-9a-f]{40}", str(provenance.get("stage4_source_commit", ""))):
            raise ValueError("provenance lacks an exact Stage4 source commit")
        if (not re.fullmatch(r"[0-9a-f]{40}", str(provenance.get("stage4_source_tree", ""))) or
                not re.fullmatch(r"[0-9a-f]{64}", str(
                    provenance.get("stage4_source_archive_sha256", "")
                ))):
            raise ValueError("provenance lacks canonical committed Stage4 source bytes")
        provenance_template = load_json(Path(__file__).resolve().parents[1] / "manifest.template.json")
        if provenance_shape(provenance) != provenance_shape(provenance_template):
            raise ValueError("provenance shape differs from the single canonical schema-2 template")
        repro = reports["reproducibility-gates.json"]
        if (provenance.get("reproducibility_clean_builds") != 2 or
                provenance.get("reproducibility_gate_sha256") != sha256(
                    source / "reproducibility-gates.json"
                ) or
                provenance.get("reproducibility_second_image_sha256") != repro.get("image_sha256")):
            raise ValueError("provenance does not cross-bind the two-clean-build hard gate")

        sums = parse_sums(source / "SHA256SUMS", HASHED_ASSETS)
        for name, expected in sums.items():
            actual = sha256(source / name)
            if actual != expected:
                raise ValueError(f"SHA256 mismatch for {name}: {actual} != {expected}")

        gate_image = gate.get("artifacts", {}).get("image", {})
        if gate.get("image") != IMAGE:
            raise ValueError("gate report names an unexpected image")
        if gate_image.get("sha256") != sums[IMAGE]:
            raise ValueError("gate-report image hash differs from SHA256SUMS")
        image_binding = provenance.get("image", {})
        if image_binding != {
            "name": IMAGE,
            "bytes": (source / IMAGE).stat().st_size,
            "sha256": sums[IMAGE],
        }:
            raise ValueError("provenance image identity/hash/size is not cross-bound")
        if provenance.get("gate_report_sha256") != sha256(source / "gate-report.json"):
            raise ValueError("provenance gate-report hash is not cross-bound")

        report_hashes = provenance.get("audit_report_sha256")
        if not isinstance(report_hashes, dict) or set(report_hashes) != set(reports):
            raise ValueError("provenance audit report map is incomplete")
        for name in reports:
            if report_hashes[name] != sha256(source / name):
                raise ValueError(f"provenance report hash differs for {name}")

        tooling_hashes = provenance.get("tooling_sha256")
        if not isinstance(tooling_hashes, dict) or set(tooling_hashes) != set(TOOLING_FILES):
            raise ValueError("provenance tooling map is incomplete")
        stage_root = Path(__file__).resolve().parents[1]
        actual_tooling_hashes: dict[str, str] = {}
        for name in TOOLING_FILES:
            path = stage_root / name
            if not is_regular_file(path):
                raise ValueError(f"current committed tooling is missing or unsafe: {name}")
            actual_tooling_hashes[name] = sha256(path)
            if tooling_hashes[name] != actual_tooling_hashes[name]:
                raise ValueError(f"current committed tooling differs from provenance: {name}")

        current_source_lock_sha256 = actual_tooling_hashes["source-lock.json"]
        output_source_lock_sha256 = sha256(source / "source-lock.json")
        if current_source_lock_sha256 != output_source_lock_sha256:
            raise ValueError("output source lock differs from the current committed source lock")
        for name in ("source-pristine-gates.json", "source-patched-gates.json"):
            if reports[name].get("source_lock_sha256") != output_source_lock_sha256:
                raise ValueError(f"{name} is not bound to the exact output source lock")

        builder = provenance.get("builder", {})
        if builder.get("base_digest") != LOCKED_BASE_DIGEST:
            raise ValueError("provenance builder base digest is not locked")
        source_lock = load_json(source / "source-lock.json")
        builder_lock = source_lock.get("builder", {})
        locked_download_closure = load_download_lock(source / "source-lock.json")
        download_manifest, download_manifest_bytes = load_manifest_with_bytes(
            source / "download-closure.json"
        )
        download_manifest_sha256 = hashlib.sha256(download_manifest_bytes).hexdigest()
        actual_download_closure = {
            "schema": download_manifest["schema"],
            "directories": len(download_manifest["directories"]),
            "files": len(download_manifest["files"]),
            "manifest_sha256": download_manifest_sha256,
        }
        if actual_download_closure != locked_download_closure:
            raise ValueError("download manifest identity differs from the exact source lock")
        if (builder.get("source_date_epoch") != builder_lock.get("source_date_epoch") or
                builder.get("apt_snapshot") != builder_lock.get("apt_snapshot") or
                builder.get("apt_snapshot_uri") != builder_lock.get("apt_snapshot_uri") or
                builder.get("apt_archive_keyring_sha256") !=
                builder_lock.get("apt_archive_keyring_sha256") or
                builder.get("ca_bootstrap") != builder_lock.get("ca_bootstrap") or
                builder.get("dockerfile_sha256") != builder_lock.get("dockerfile_sha256") or
                builder.get("apt_sources_sha256") != builder_lock.get("apt_sources_sha256")):
            raise ValueError("provenance builder snapshot/tooling identity differs from source lock")
        identity_lock = source_lock.get("required_image_identity", {})
        expected_image_identity = {
            "device": identity_lock.get("device"),
            "compat_version": identity_lock.get("compat_version"),
            "layout": "112 MiB single-UBI",
            "fixed_partitions_path": identity_lock.get("fixed_partitions_path"),
            "compiled_partition_order": identity_lock.get("compiled_partition_order"),
        }
        if (provenance.get("source_date_epoch") != builder_lock.get("source_date_epoch") or
                provenance.get("openwrt_commit") != source_lock.get("openwrt", {}).get("commit") or
                provenance.get("openwrt_revision") != source_lock.get("openwrt", {}).get("revision") or
                provenance.get("kwrt_layout_commit") !=
                source_lock.get("kwrt_layout_source", {}).get("commit") or
                provenance.get("kwrt_vermagic_transform_sha256") !=
                source_lock.get("patches", {}).get("10-kwrt-vermagic-one.patch") or
                provenance.get("mt76_commit") != source_lock.get("mt76", {}).get("commit") or
                provenance.get("capture_commit") != source_lock.get("capture", {}).get("commit") or
                provenance.get("capture_stage2_behavior_commit") !=
                source_lock.get("capture", {}).get("stage2_behavior_commit") or
                provenance.get("capture_source_archive_bytes") !=
                source_lock.get("capture", {}).get("source_archive_bytes") or
                provenance.get("capture_source_archive_sha256") !=
                source_lock.get("capture", {}).get("source_archive_sha256") or
                provenance.get("capture_source_archive_toolchain") !=
                source_lock.get("capture", {}).get("canonical_toolchain") or
                provenance.get("capture_stage3_validation") !=
                source_lock.get("capture", {}).get("stage3_validation") or
                provenance.get("image_identity") != expected_image_identity):
            raise ValueError("provenance source/image identity differs from source lock")
        abi_lock = source_lock.get("live_abi_baseline", {})
        builder_vanilla_lock = source_lock.get("canonical_builder_vanilla", {})
        if (not isinstance(builder_vanilla_lock, dict) or
                set(builder_vanilla_lock) != {"module_bytes", "module_sha256"} or
                type(builder_vanilla_lock.get("module_bytes")) is not int or
                builder_vanilla_lock["module_bytes"] <= 0 or
                re.fullmatch(
                    r"[0-9a-f]{64}", str(builder_vanilla_lock.get("module_sha256", ""))
                ) is None):
            raise ValueError("canonical builder vanilla lock schema/identity is invalid")
        expected_vanilla_module = {
            "bytes": builder_vanilla_lock.get("module_bytes"),
            "sha256": builder_vanilla_lock.get("module_sha256"),
        }
        vanilla_path = source / "mt7915e.vanilla.ko"
        vanilla_gate_artifact = gate.get("artifacts", {}).get(
            "mt7915e_vanilla_module", {}
        )
        vanilla_report_artifact = reports["vanilla-abi-gates.json"].get(
            "artifacts", {}
        ).get("mt7915e_vanilla_module", {})
        if (provenance.get("kernel_build_identity") != "builder@buildhost" or
                provenance.get("kernel_package_format") != abi_lock.get("package_format") or
                provenance.get("kernel_dependency") != abi_lock.get("kernel_dependency") or
                provenance.get("this_module_section_size") !=
                abi_lock.get("this_module_section_size") or
                provenance.get("vanilla_undefined_symbols_count") !=
                abi_lock.get("undefined_symbols_count") or
                provenance.get("vanilla_undefined_symbols_sha256") !=
                abi_lock.get("undefined_symbols_sha256")):
            raise ValueError("provenance kernel/module ABI identity differs from source lock")
        if (not is_regular_file(vanilla_path) or
                expected_vanilla_module["bytes"] != vanilla_path.stat().st_size or
                expected_vanilla_module["sha256"] != sha256(vanilla_path) or
                provenance.get("vanilla_module") != expected_vanilla_module or
                vanilla_gate_artifact.get("bytes") != expected_vanilla_module["bytes"] or
                vanilla_gate_artifact.get("sha256") != expected_vanilla_module["sha256"] or
                vanilla_report_artifact.get("bytes") != expected_vanilla_module["bytes"] or
                vanilla_report_artifact.get("sha256") != expected_vanilla_module["sha256"]):
            raise ValueError(
                "canonical builder vanilla module differs from source lock, provenance, "
                "vanilla report, or final gate"
            )
        if (provenance.get("capture_default_enabled") is not False or
                provenance.get("wireless_config_preseeded") is not False or
                provenance.get("preserved_wireless_config_mutated") is not False or
                provenance.get("runtime_board_detection_executed") is not False):
            raise ValueError("provenance network-safe defaults are not fail-closed")
        artifact_hash_fields = {
            "capture_source_gate_sha256": "capture-source-gates.json",
            "source_lock_sha256": "source-lock.json",
            "kwrt_exact_config_sha256": "kwrt-exact.config",
            "build_config_sha256": "build.config",
            "kernel_config_sha256": "kernel.config",
            "module_symvers_sha256": "Module.symvers",
            "build_log_sha256": "build.log",
            "package_manifest_sha256": "packages.manifest",
        }
        for field, name in artifact_hash_fields.items():
            if provenance.get(field) != sha256(source / name):
                raise ValueError(f"provenance artifact hash differs for {name}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(builder.get("image_id", ""))):
            raise ValueError("provenance builder image ID is invalid")
        if type(builder.get("jobs")) is not int or builder["jobs"] not in range(1, 7):
            raise ValueError("provenance build parallelism is outside 1..6")
        if builder.get("package_versions_sha256") != sha256(source / "builder-packages.txt"):
            raise ValueError("builder package-version manifest is not cross-bound")
        package_versions = (source / "builder-packages.txt").read_text(encoding="utf-8").splitlines()
        if builder.get("package_versions") != package_versions:
            raise ValueError("public provenance does not embed the exact builder package list")
        if builder.get("build_network") != "none":
            raise ValueError("provenance does not prove a network-disabled build phase")
        if builder.get("networked_prepare_receipt_sha256") != sha256(
            source / "network-prepare-receipt.json"
        ):
            raise ValueError("networked prepare receipt is not cross-bound")
        if builder.get("download_manifest_sha256") != download_manifest_sha256:
            raise ValueError("download manifest is not cross-bound")
        prepare_receipt = load_json(source / "network-prepare-receipt.json")
        if type(prepare_receipt.get("schema")) is not int:
            raise ValueError("networked prepare receipt schema is not an integer")
        expected_prepare_receipt = {
            "schema": 1,
            "phase": "networked-prepare-complete",
            "stage4_source_commit": provenance.get("stage4_source_commit"),
            "stage4_source_tree": provenance.get("stage4_source_tree"),
            "stage4_source_archive_sha256": provenance.get("stage4_source_archive_sha256"),
            "builder_image_id": builder.get("image_id"),
            "builder_base_digest": builder.get("base_digest"),
            "openwrt_commit": provenance.get("openwrt_commit"),
            "kwrt_commit": provenance.get("kwrt_layout_commit"),
            "final_config_sha256": sha256(source / "build.config"),
            "download_manifest_sha256": download_manifest_sha256,
        }
        if prepare_receipt != expected_prepare_receipt:
            raise ValueError("networked prepare receipt identity differs from locked provenance")

        signature = gate_artifacts.get("signature", {})
        if provenance.get("signature") != signature:
            raise ValueError("provenance signature evidence differs from final gate evidence")
        signing_lock = source_lock.get("signing", {})
        if (signing_lock.get("status") != "READY" or
                signature.get("public_key_sha256") != sha256(source / "ax3000t-stage4.pub") or
                signature.get("base_ucert_sha256") != sha256(source / "ax3000t-stage4.ucert") or
                signature.get("public_key_sha256") != signing_lock.get("public_key_sha256") or
                signature.get("base_ucert_sha256") != signing_lock.get("base_ucert_sha256") or
                signature.get("usign_fingerprint") != signing_lock.get("usign_fingerprint") or
                signature.get("base_ucert_validfrom") !=
                signing_lock.get("base_ucert_validfrom") or
                signature.get("base_ucert_expiresat") !=
                signing_lock.get("base_ucert_expiresat")):
            raise ValueError("signature evidence is not cross-bound to the pinned public key/cert lock")

        audit_sums = parse_sums(source / "AUDIT-SHA256SUMS", AUDIT_ASSETS)
        for name, expected in audit_sums.items():
            if sha256(source / name) != expected:
                raise ValueError(f"AUDIT-SHA256SUMS mismatch for {name}")

        copied_assets = write_release_bundle(
            source,
            bundle,
            RELEASE_ASSETS,
            {name: snapshot_identities[name] for name in RELEASE_ASSETS},
        )
        print(json.dumps({
            "classification": "EXPERIMENTAL-DO-NOT-FLASH",
            "result": "pass",
            "flash_authorized": False,
            "assets": copied_assets,
        }, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"release bundle rejected: {exc}", file=sys.stderr)
        return 1
    finally:
        if snapshot_owner is not None:
            snapshot_owner.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
