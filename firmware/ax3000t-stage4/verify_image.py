#!/usr/bin/env python3
"""Offline release gates for the AX3000T 112 MiB single-UBI image.

The verifier is deliberately read-only.  It understands OpenWrt sysupgrade
tarballs, fwtool metadata trailers, U-Boot FIT images, and embedded flattened
device trees without third-party Python modules.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


EXPECTED_DEVICE = "xiaomi,mi-router-ax3000t"
EXPECTED_BOARD = "xiaomi_mi-router-ax3000t"
EXPECTED_TARGET = "mediatek/filogic"
EXPECTED_UBI_OFFSET = 0x00600000
EXPECTED_UBI_SIZE = 0x07000000
EXPECTED_KF_OFFSET = 0x07600000
EXPECTED_KF_SIZE = 0x00040000
EXPECTED_PARTITIONS_PATH = "/soc/spi@1100a000/flash@0/partitions"
EXPECTED_PARTITIONS = (
    ("partition@0", "BL2", 0x00000000, 0x00100000, True),
    ("partition@100000", "Nvram", 0x00100000, 0x00040000, False),
    ("partition@140000", "Bdata", 0x00140000, 0x00040000, False),
    ("partition@180000", "Factory", 0x00180000, 0x00200000, True),
    ("partition@380000", "FIP", 0x00380000, 0x00200000, True),
    ("partition@580000", "crash", 0x00580000, 0x00040000, True),
    ("partition@5c0000", "crash_log", 0x005C0000, 0x00040000, True),
    # The compiled DT preserves the base-DTS KF node before the board-DTS UBI
    # override. Keep this order locked because it determines mtd7/mtd8.
    ("partition@7600000", "KF", 0x07600000, 0x00040000, True),
    ("partition@600000", "ubi", 0x00600000, 0x07000000, False),
)
EXPECTED_KERNEL_RELEASE = "6.12.94"
EXPECTED_MT76_REV = "39c960c3"
EXPECTED_VERMAGIC = "6.12.94 SMP mod_unload aarch64"
EXPECTED_KERNEL_DEPENDENCY = "kernel (=6.12.94~1-r1)"
EXPECTED_KERNEL_PACKAGE_VERSION = "6.12.94~1-r1"
EXPECTED_MT7915E_PACKAGE_VERSION = "6.12.94.2026.03.19~39c960c3-r2"
EXPECTED_CAPTURE_PACKAGE_VERSION = "2.0.0~git20260830.b8d7b73-r1"
EXPECTED_THIS_MODULE_SIZE = 0x440
EXPECTED_BASELINE_UNDEFINED_COUNT = 294
EXPECTED_BASELINE_UNDEFINED_SHA256 = (
    "a17a1bbec220f58147a40693cc8f1b1f8079b787f6eb7a9461eb9e4b352d10fb"
)
EXPECTED_BASELINE_MODULE_BYTES = 218088
EXPECTED_BASELINE_MODULE_SHA256 = (
    "346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621"
)
EXPECTED_PATCHED_UNDEFINED_COUNT = 297
EXPECTED_PATCHED_UNDEFINED_SHA256 = (
    "9682dffc0a1dc4760fb7bb61f6c5d1b8439a7f561e191f9ab5804dd4a9aadc4d"
)
EXPECTED_PATCHED_UNDEFINED_DELTA = {"__nla_parse", "nla_put", "skb_trim"}
EXPECTED_COMPAT_VERSION = "2.0"
EXPECTED_VERSION_DIST = "OpenWrt-CSI-Lab"
EXPECTED_VERSION_NUMBER = "25.12.5-experimental"
EXPECTED_REVISION = "r33051-f5dae5ece4"
EXPECTED_COMPAT_MESSAGE = (
    "EXPERIMENTAL: verified 112 MiB single-UBI only; stock dual-UBI must never be forced. "
    "实验镜像：仅适用于已核验的 112 MiB single-UBI；stock dual-UBI 绝不可强制刷入。"
)
EXPECTED_LEGACY_SUPPORTED_MESSAGE = (
    f"{EXPECTED_DEVICE} - Image version mismatch: image {EXPECTED_COMPAT_VERSION}, "
    "device 1.0. Please wipe config during upgrade (force required) or reinstall. "
    f"Reason: {EXPECTED_COMPAT_MESSAGE}"
)
EXPECTED_WIFI_GENERATOR_SHA256 = (
    "84ecf265e27e59b1a35fdcef45d06ffd015617678d66173fb1fb80fbfedebfc4"
)
EXPECTED_WIFI_DISABLED_EXPRESSION = "set ${si}.disabled='${defaults ? 0 : 1}'"
EXPECTED_WIFI_EMPTY_KEY_EXPRESSION = "set ${si}.key='${defaults?.key || \"\"}'"
EXPECTED_PLATFORM_SHA256 = (
    "940469508bd11bf595f24bf0ad313de68ee41d9a65643f2290a217d2c54127ec"
)
EXPECTED_CAPTURE_CONFIG_SHA256 = (
    "2d9407740ce1051f0403c423f01a951083d692095a1ee52ff7705b8bf9feabda"
)
EXPECTED_CAPTURE_INIT_SHA256 = (
    "9691048cea2118702a0b2e012950c82acdbfedb662be3caadee493c58ca5f29a"
)
FDT_MAGIC = 0xD00DFEED


class VerificationError(RuntimeError):
    pass


@dataclass
class Gate:
    name: str
    status: str
    detail: str
    evidence: Any = None


class Report:
    def __init__(self, image: Path) -> None:
        self.image = image
        self.gates: list[Gate] = []
        self.artifacts: dict[str, Any] = {}

    def passed(self, name: str, detail: str, evidence: Any = None) -> None:
        self.gates.append(Gate(name, "pass", detail, evidence))

    def failed(self, name: str, detail: str, evidence: Any = None) -> None:
        self.gates.append(Gate(name, "fail", detail, evidence))

    def warned(self, name: str, detail: str, evidence: Any = None) -> None:
        self.gates.append(Gate(name, "warn", detail, evidence))

    def require(self, condition: bool, name: str, detail: str,
                evidence: Any = None) -> bool:
        if condition:
            self.passed(name, detail, evidence)
            return True
        self.failed(name, detail, evidence)
        return False

    @property
    def ok(self) -> bool:
        return not any(g.status == "fail" for g in self.gates)

    def as_json(self) -> dict[str, Any]:
        if any(g.status == "fail" for g in self.gates):
            result = "fail"
        elif any(g.status == "warn" for g in self.gates):
            result = "incomplete"
        else:
            result = "pass"
        return {
            "schema": 1,
            "classification": "EXPERIMENTAL-DO-NOT-FLASH",
            "flash_authorized": False,
            # This JSON is a public release artifact.  A basename proves the
            # selected asset without leaking a builder username or checkout.
            "image": self.image.name,
            "result": result,
            "artifacts": self.artifacts,
            "gates": [asdict(g) for g in self.gates],
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def safe_root_path(root: Path, relative: str) -> Path:
    """Return a path only when no existing component is a symlink.

    Rootfs evidence must never escape the controlled unsquashfs directory via
    a symlinked ancestor such as etc/config or lib/modules. Missing components
    are returned normally so the caller can apply its own required/optional
    policy without following anything outside ``root``.
    """
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise VerificationError(f"unsafe rootfs-relative path: {relative!r}")
    if root.is_symlink() or not root.is_dir():
        raise VerificationError("rootfs root is missing, non-directory, or a symlink")
    current = root
    for index, part in enumerate(rel.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return current.joinpath(*rel.parts[index + 1:])
        except OSError as exc:
            raise VerificationError(f"cannot lstat rootfs path {relative!r}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise VerificationError(f"rootfs path has symlink component: {current.relative_to(root)}")
    return current


def c_string(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("utf-8", "replace")


def safe_tar_name(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and bool(p.parts)


def read_sysupgrade(path: Path, report: Report) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    roots: set[str] = set()
    last_member_end = 0
    try:
        with tarfile.open(path, "r:") as tf:
            for member in tf.getmembers():
                if not safe_tar_name(member.name):
                    raise VerificationError(f"unsafe tar member: {member.name!r}")
                if member.issym() or member.islnk() or member.isdev():
                    raise VerificationError(f"unsupported tar member type: {member.name!r}")
                parts = PurePosixPath(member.name).parts
                if parts:
                    roots.add(parts[0])
                last_member_end = max(
                    last_member_end,
                    member.offset_data + ((member.size + 511) // 512) * 512,
                )
                if not member.isfile():
                    continue
                leaf = parts[-1]
                if leaf not in {"CONTROL", "kernel", "root"}:
                    raise VerificationError(f"unexpected file in sysupgrade tar: {member.name!r}")
                if leaf in payloads:
                    raise VerificationError(f"duplicate payload: {leaf}")
                src = tf.extractfile(member)
                if src is None:
                    raise VerificationError(f"cannot read payload: {member.name!r}")
                payloads[leaf] = src.read()
    except (tarfile.TarError, OSError) as exc:
        raise VerificationError(f"invalid sysupgrade tar: {exc}") from exc

    try:
        raw = path.read_bytes()
        chunks = fwtool_chunks(raw)
    except OSError as exc:
        raise VerificationError(f"cannot read raw sysupgrade bytes: {exc}") from exc
    base_end = chunks[-1]["start"] if chunks else len(raw)
    if base_end < last_member_end:
        raise VerificationError("fwtool chunks overlap a tar member")
    tar_tail = raw[last_member_end:base_end]
    # Locked OpenWrt sysupgrade tar output ends on a 10 KiB record boundary.
    # At least two 512-byte zero blocks terminate the archive and every byte
    # between the last member and the first fwtool chunk is zero padding.  This
    # rejects data hidden after tar EOF but before otherwise-valid metadata.
    report.require(
        base_end % tarfile.RECORDSIZE == 0 and
        len(tar_tail) >= 1024 and
        tar_tail == bytes(len(tar_tail)),
        "sysupgrade.tar.closure",
        "tar ends with canonical all-zero 10 KiB record padding immediately before fwtool chunks",
        {
            "last_member_padded_end": last_member_end,
            "fwtool_base_end": base_end,
            "padding_bytes": len(tar_tail),
            "nonzero_padding_bytes": sum(value != 0 for value in tar_tail),
        },
    )

    report.require(
        roots == {f"sysupgrade-{EXPECTED_BOARD}"},
        "sysupgrade.tar.root",
        "tar uses the exact AX3000T board directory",
        sorted(roots),
    )
    report.require(
        set(payloads) == {"CONTROL", "kernel", "root"},
        "sysupgrade.tar.members",
        "CONTROL, kernel, and root are present exactly once",
        sorted(payloads),
    )
    for name, data in payloads.items():
        report.artifacts[name] = {"bytes": len(data), "sha256": sha256_bytes(data)}

    control = payloads.get("CONTROL", b"").decode("ascii", "replace").strip()
    report.require(
        control == f"BOARD={EXPECTED_BOARD}",
        "sysupgrade.control.board",
        "CONTROL selects only the AX3000T board",
        control,
    )

    combined = len(payloads.get("kernel", b"")) + len(payloads.get("root", b""))
    conservative_limit = EXPECTED_UBI_SIZE - (8 * 1024 * 1024)
    report.require(
        combined <= conservative_limit,
        "ubi.payload.capacity",
        "kernel + root fit below the 112 MiB UBI partition with an 8 MiB reserve",
        {
            "combined_bytes": combined,
            "partition_bytes": EXPECTED_UBI_SIZE,
            "reserve_bytes": EXPECTED_UBI_SIZE - combined,
            "minimum_reserve_bytes": 8 * 1024 * 1024,
        },
    )
    return payloads


def fwtool_chunks(image: bytes) -> list[dict[str, Any]]:
    """Read and CRC-check fwtool chunks from the tail, newest first."""
    chunks: list[dict[str, Any]] = []
    end = len(image)
    while end >= 16:
        trailer = image[end - 16:end]
        magic, crc, kind, size = struct.unpack(">4sIB3xI", trailer)
        if magic != b"FWx0":
            break
        if size < 16 or size > end:
            raise VerificationError(f"invalid fwtool chunk size {size}")
        start = end - size
        calculated = (zlib.crc32(image[:end - 16]) ^ 0xFFFFFFFF) & 0xFFFFFFFF
        if calculated != crc:
            raise VerificationError(
                f"fwtool CRC mismatch: stored={crc:08x} calculated={calculated:08x}"
            )
        chunks.append({
            "type": kind,
            "size": size,
            "crc32": f"{crc:08x}",
            "payload": image[start:end - 16],
            "start": start,
            "end": end,
        })
        end = start
    return chunks


def verify_metadata(image: bytes, report: Report) -> dict[str, Any] | None:
    try:
        chunks = fwtool_chunks(image)
    except VerificationError as exc:
        report.failed("metadata.fwtool.crc", str(exc))
        return None
    report.require(bool(chunks), "metadata.fwtool.present",
                   "a valid fwtool metadata trailer is present")
    info_chunks = [chunk for chunk in chunks if chunk["type"] == 1]
    signature_chunks = [chunk for chunk in chunks if chunk["type"] == 0]
    report.require(
        [chunk["type"] for chunk in chunks] == [0, 1] and
        len(info_chunks) == 1 and len(signature_chunks) == 1 and
        0 < len(signature_chunks[0]["payload"]) <= 1024 and
        8 <= len(info_chunks[0]["payload"]) <= 30 * 1024 + 8,
        "metadata.fwtool.unique_info",
        "runtime-visible fwtool tail is exactly one newest SIGNATURE followed by one INFO",
        {"chunk_types_newest_first": [chunk["type"] for chunk in chunks],
         "info_count": len(info_chunks),
         "signature_count": len(signature_chunks),
         "signature_bytes": [len(chunk["payload"]) for chunk in signature_chunks]},
    )
    metadata: dict[str, Any] | None = None
    chunk_summary = [
        {k: v for k, v in chunk.items() if k != "payload"} for chunk in chunks
    ]
    # fwtool_chunks is newest-first, matching fwtool's runtime selection. Even
    # though duplicates are a hard failure above, parse only that same newest
    # INFO so diagnostic evidence can never describe an older shadowed chunk.
    if info_chunks:
        payload = info_chunks[0]["payload"]
        if len(payload) < 8:
            report.failed("metadata.fwtool.header", "metadata payload is shorter than its header")
        else:
            version, flags = struct.unpack(">II", payload[:8])
            report.require(version == 0 and flags == 0, "metadata.fwtool.header",
                           "fwtool metadata header version and flags are zero",
                           {"version": version, "flags": flags})
            try:
                value = json.loads(payload[8:].decode("utf-8"))
                if isinstance(value, dict):
                    metadata = value
                else:
                    report.failed("metadata.json", "metadata JSON is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                report.failed("metadata.json", f"metadata JSON is invalid: {exc}")
    report.artifacts["fwtool_chunks"] = chunk_summary
    if metadata is None:
        report.failed("metadata.json", "no fwtool information chunk was found")
        return None

    expected_top_level_keys = {
        "metadata_version", "compat_version", "compat_message",
        "new_supported_devices", "supported_devices", "version",
    }
    report.require(
        set(metadata) == expected_top_level_keys,
        "metadata.schema",
        "metadata has exactly the locked OpenWrt compat-2.0 fields",
        sorted(metadata),
    )
    report.require(
        metadata.get("metadata_version") == "1.1",
        "metadata.metadata_version",
        "metadata uses OpenWrt schema 1.1 required for new_supported_devices",
        metadata.get("metadata_version"),
    )
    version = metadata.get("version", {})
    report.require(
        metadata.get("new_supported_devices") == [EXPECTED_DEVICE],
        "metadata.supported_devices",
        "compat-2.0 runtime selection supports exactly the AX3000T identifier",
        metadata.get("new_supported_devices"),
    )
    report.require(
        metadata.get("supported_devices") == [EXPECTED_LEGACY_SUPPORTED_MESSAGE],
        "metadata.legacy_supported_message",
        "legacy readers receive the exact forced-migration warning rather than a device identifier",
        metadata.get("supported_devices"),
    )
    report.require(
        str(metadata.get("compat_version")) == EXPECTED_COMPAT_VERSION,
        "metadata.compat_version",
        "compat version is raised above stock 1.0 to block an ordinary dual-UBI upgrade",
        metadata.get("compat_version"),
    )
    report.require(
        metadata.get("compat_message") == EXPECTED_COMPAT_MESSAGE,
        "metadata.compat_message",
        "metadata carries the exact bilingual single-UBI-only / never-force warning",
        metadata.get("compat_message"),
    )
    report.require(
        isinstance(version, dict) and set(version) == {
            "dist", "version", "revision", "target", "board"
        },
        "metadata.version.schema",
        "version object has exactly the locked OpenWrt identity fields",
        sorted(version) if isinstance(version, dict) else type(version).__name__,
    )
    report.require(
        isinstance(version, dict) and
        version.get("dist") == EXPECTED_VERSION_DIST and
        version.get("version") == EXPECTED_VERSION_NUMBER and
        version.get("revision") == EXPECTED_REVISION,
        "metadata.version.identity",
        "metadata distribution, release number, and source revision are exactly pinned",
        {
            "dist": version.get("dist") if isinstance(version, dict) else None,
            "version": version.get("version") if isinstance(version, dict) else None,
            "revision": version.get("revision") if isinstance(version, dict) else None,
        },
    )
    report.require(
        isinstance(version, dict) and
        version.get("target") == EXPECTED_TARGET,
        "metadata.target",
        "metadata target is MediaTek Filogic",
        version.get("target") if isinstance(version, dict) else None,
    )
    report.require(
        isinstance(version, dict) and version.get("board") == EXPECTED_BOARD,
        "metadata.board",
        "metadata board is Xiaomi AX3000T",
        version.get("board") if isinstance(version, dict) else None,
    )
    report.artifacts["metadata"] = metadata
    return metadata


def verify_image_signature(image: bytes, root: Path, *, ucert: Path, usign: Path,
                           public_key: Path, base_ucert: Path,
                           source_lock: Path, report: Report) -> None:
    """Verify the fwtool signature against an independently locked public key."""
    inputs = {
        "ucert": ucert,
        "usign": usign,
        "public_key": public_key,
        "base_ucert": base_ucert,
        "source_lock": source_lock,
    }
    invalid = [name for name, path in inputs.items() if not is_regular_file(path)]
    nonexec = [name for name in ("ucert", "usign") if not os.access(inputs[name], os.X_OK)]
    if invalid or nonexec:
        report.failed(
            "signature.inputs",
            "signature verifier inputs are missing, non-regular, symlinked, or non-executable",
            {"invalid": invalid, "nonexecutable": nonexec},
        )
        return
    report.passed(
        "signature.inputs",
        "signature tools, public key, base ucert, and source lock are regular non-symlink inputs",
        {name: path.name for name, path in inputs.items()},
    )
    try:
        lock = json.loads(source_lock.read_text(encoding="utf-8"))
        signing = lock["signing"]
        public_data = public_key.read_bytes()
        base_cert_data = base_ucert.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        report.failed("signature.inputs", f"cannot read signing lock: {exc}")
        return
    locked = {
        "status": signing.get("status"),
        "public_key_sha256": signing.get("public_key_sha256"),
        "base_ucert_sha256": signing.get("base_ucert_sha256"),
        "usign_fingerprint": signing.get("usign_fingerprint"),
        "base_ucert_validfrom": signing.get("base_ucert_validfrom"),
        "base_ucert_expiresat": signing.get("base_ucert_expiresat"),
    }
    hashes_match = (
        locked["status"] == "READY" and
        re.fullmatch(r"[0-9a-f]{64}", str(locked["public_key_sha256"] or "")) is not None and
        re.fullmatch(r"[0-9a-f]{64}", str(locked["base_ucert_sha256"] or "")) is not None and
        sha256_bytes(public_data) == locked["public_key_sha256"] and
        sha256_bytes(base_cert_data) == locked["base_ucert_sha256"]
    )
    report.require(
        hashes_match,
        "signature.public_inputs",
        "pinned public key and time-bearing base ucert match the READY source lock",
        {
            "status": locked["status"],
            "public_key_sha256": sha256_bytes(public_data),
            "base_ucert_sha256": sha256_bytes(base_cert_data),
        },
    )
    verifier_env = os.environ.copy()
    verifier_env["PATH"] = str(usign.parent) + os.pathsep + verifier_env.get("PATH", "")
    try:
        base_verify_proc = subprocess.run(
            [str(ucert), "-q", "-V", "-c", str(base_ucert),
             "-p", str(public_key)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env=verifier_env,
        )
        base_dump_proc = subprocess.run(
            [str(ucert), "-D", "-c", str(base_ucert)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env=verifier_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.failed("signature.base_ucert_identity", f"base ucert inspection failed: {exc}")
        return
    validfrom_matches = re.findall(r'"validfrom"\s*:\s*([0-9]+)', base_dump_proc.stdout)
    expiresat_matches = re.findall(r'"expiresat"\s*:\s*([0-9]+)', base_dump_proc.stdout)
    actual_validfrom = int(validfrom_matches[0]) if len(validfrom_matches) == 1 else None
    actual_expiresat = int(expiresat_matches[0]) if len(expiresat_matches) == 1 else None
    cert_identity_ok = (
        base_verify_proc.returncode == 0 and base_dump_proc.returncode == 0 and
        actual_validfrom == locked["base_ucert_validfrom"] and
        actual_expiresat == locked["base_ucert_expiresat"] and
        isinstance(actual_validfrom, int) and isinstance(actual_expiresat, int) and
        actual_expiresat > actual_validfrom
    )
    report.require(
        cert_identity_ok,
        "signature.base_ucert_identity",
        "the pinned base ucert cryptographically validates and exposes the exact locked validity window",
        {
            "verified": base_verify_proc.returncode == 0,
            "validfrom": actual_validfrom,
            "expiresat": actual_expiresat,
        },
    )
    if not cert_identity_ok:
        return
    try:
        fingerprint_proc = subprocess.run(
            [str(usign), "-F", "-p", str(public_key)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
            env=verifier_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.failed("signature.fingerprint", f"usign fingerprint failed: {exc}")
        return
    fingerprint = fingerprint_proc.stdout.strip().splitlines()[-1].strip() \
        if fingerprint_proc.stdout.strip() else ""
    report.require(
        fingerprint_proc.returncode == 0 and
        re.fullmatch(r"[0-9a-fA-F]{16,64}", fingerprint) is not None and
        fingerprint.lower() == str(locked["usign_fingerprint"] or "").lower(),
        "signature.fingerprint",
        "independently pinned public key has the locked usign fingerprint",
        {"fingerprint": fingerprint.lower() if fingerprint else None},
    )
    try:
        chunks = fwtool_chunks(image)
    except VerificationError as exc:
        report.failed("signature.crypto", str(exc))
        return
    if [chunk["type"] for chunk in chunks] != [0, 1]:
        report.failed(
            "signature.crypto",
            "cannot verify anything except the exact SIGNATURE, INFO fwtool layout",
        )
        return
    signature_chunk = chunks[0]
    signed_prefix = image[:signature_chunk["start"]]
    signature_payload = signature_chunk["payload"]
    report.require(
        len(signature_payload) > len(base_cert_data) and
        signature_payload.startswith(base_cert_data),
        "signature.base_ucert_prefix",
        "image certificate chain begins with the exact pinned time-bearing base ucert",
        {
            "base_ucert_bytes": len(base_cert_data),
            "signature_payload_bytes": len(signature_payload),
            "prefix_matches": signature_payload.startswith(base_cert_data),
        },
    )
    with tempfile.TemporaryDirectory(prefix="ax3000t-stage4-signature-") as temp:
        temp_path = Path(temp)
        message_path = temp_path / "signed-prefix.bin"
        cert_path = temp_path / "image.ucert"
        message_path.write_bytes(signed_prefix)
        cert_path.write_bytes(signature_payload)
        try:
            verify_proc = subprocess.run(
                [str(ucert), "-q", "-V", "-m", str(message_path),
                 "-c", str(cert_path), "-p", str(public_key)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
                env=verifier_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.failed("signature.crypto", f"ucert verification failed: {exc}")
            return
    report.require(
        verify_proc.returncode == 0,
        "signature.crypto",
        "fwtool signature validates the exact tar+INFO prefix under the independently pinned key",
        {"verified": verify_proc.returncode == 0},
    )
    try:
        root_key = safe_root_path(root, f"etc/opkg/keys/{fingerprint.lower()}")
        root_key_data = root_key.read_bytes() if is_regular_file(root_key) else b""
    except (VerificationError, OSError) as exc:
        report.failed("signature.rootfs_trust", str(exc))
        root_key_data = b""
    else:
        report.require(
            bool(fingerprint) and root_key_data == public_data,
            "signature.rootfs_trust",
            "final rootfs trusts the exact pinned Stage4 public key under its fingerprint",
            {
                "path": f"etc/opkg/keys/{fingerprint.lower()}",
                "present": bool(root_key_data),
                "sha256": sha256_bytes(root_key_data) if root_key_data else None,
            },
        )
    report.artifacts["signature"] = {
        "signed_prefix_bytes": len(signed_prefix),
        "signed_prefix_sha256": sha256_bytes(signed_prefix),
        "signature_chunk_bytes": len(signature_payload),
        "signature_chunk_sha256": sha256_bytes(signature_payload),
        "public_key_sha256": sha256_bytes(public_data),
        "base_ucert_sha256": sha256_bytes(base_cert_data),
        "usign_fingerprint": fingerprint.lower() if fingerprint else None,
        "base_ucert_validfrom": actual_validfrom,
        "base_ucert_expiresat": actual_expiresat,
    }


def parse_fdt(blob: bytes) -> dict[str, dict[str, bytes]]:
    if len(blob) < 40:
        raise VerificationError("FDT is shorter than its header")
    (magic, total, off_struct, off_strings, _off_mem, version, last_compat,
     _boot_cpu, size_strings, size_struct) = struct.unpack_from(">10I", blob, 0)
    if magic != FDT_MAGIC:
        raise VerificationError("not an FDT/FIT blob")
    if total > len(blob) or version < 16 or last_compat > version:
        raise VerificationError("invalid FDT header bounds/version")
    if off_struct + size_struct > total or off_strings + size_strings > total:
        raise VerificationError("FDT section is outside totalsize")

    strings = blob[off_strings:off_strings + size_strings]
    pos = off_struct
    end = off_struct + size_struct
    stack: list[str] = []
    nodes: dict[str, dict[str, bytes]] = {}
    saw_end = False

    while pos + 4 <= end:
        token = struct.unpack_from(">I", blob, pos)[0]
        pos += 4
        if token == 1:  # FDT_BEGIN_NODE
            try:
                nul = blob.index(0, pos, end)
            except ValueError as exc:
                raise VerificationError("unterminated FDT node name") from exc
            name = blob[pos:nul].decode("utf-8", "replace")
            pos = (nul + 4) & ~3
            stack.append(name)
            path = "/" + "/".join(part for part in stack if part)
            if path in nodes:
                raise VerificationError(f"duplicate FDT node {path}")
            nodes[path] = {}
        elif token == 2:  # FDT_END_NODE
            if not stack:
                raise VerificationError("unbalanced FDT END_NODE")
            stack.pop()
        elif token == 3:  # FDT_PROP
            if pos + 8 > end or not stack:
                raise VerificationError("invalid FDT property header")
            length, name_offset = struct.unpack_from(">II", blob, pos)
            pos += 8
            if pos + length > end or name_offset >= len(strings):
                raise VerificationError("invalid FDT property bounds")
            try:
                name_end = strings.index(0, name_offset)
            except ValueError as exc:
                raise VerificationError("unterminated FDT property name") from exc
            name = strings[name_offset:name_end].decode("utf-8", "replace")
            data = blob[pos:pos + length]
            pos = (pos + length + 3) & ~3
            path = "/" + "/".join(part for part in stack if part)
            if name in nodes[path]:
                raise VerificationError(f"duplicate FDT property {path}:{name}")
            nodes[path][name] = data
        elif token == 4:  # FDT_NOP
            continue
        elif token == 9:  # FDT_END
            saw_end = True
            break
        else:
            raise VerificationError(f"unknown FDT token {token} at offset {pos - 4:#x}")
    if not saw_end or stack:
        raise VerificationError("incomplete or unbalanced FDT structure")
    return nodes


def verify_fit_hashes(nodes: dict[str, dict[str, bytes]],
                      report: Report) -> dict[str, list[dict[str, str]]]:
    checked: list[dict[str, str]] = []
    failures: list[str] = []
    verified: dict[str, list[dict[str, str]]] = {}
    for path, props in nodes.items():
        if not path.startswith("/images/") or path.count("/") != 2 or "data" not in props:
            continue
        data = props["data"]
        for hash_path, hash_props in nodes.items():
            if not hash_path.startswith(path + "/") or not hash_path.rsplit("/", 1)[-1].startswith("hash-"):
                continue
            if hash_path.rsplit("/", 1)[0] != path:
                failures.append(f"{hash_path}: hash node is not a direct image child")
                continue
            algo = c_string(hash_props.get("algo", b""))
            expected = hash_props.get("value", b"")
            if algo == "crc32":
                actual = struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
            elif algo == "sha1":
                actual = hashlib.sha1(data).digest()
            elif algo == "sha256":
                actual = hashlib.sha256(data).digest()
            else:
                failures.append(f"{hash_path}: unsupported algorithm {algo!r}")
                continue
            checked.append({"path": hash_path, "algorithm": algo,
                            "value": expected.hex()})
            if actual != expected:
                failures.append(f"{hash_path}: digest mismatch")
            else:
                verified.setdefault(path, []).append(
                    {"path": hash_path, "algorithm": algo}
                )
    report.require(bool(checked) and not failures, "fit.payload.hashes",
                   "all advertised FIT payload hashes verify",
                   {"checked": checked, "failures": failures})
    return verified


def u32_pair(data: bytes) -> tuple[int, int] | None:
    if len(data) != 8:
        return None
    return struct.unpack(">II", data)


def verify_dtb(kernel: bytes, report: Report) -> None:
    try:
        fit = parse_fdt(kernel)
    except VerificationError as exc:
        report.failed("fit.parse", str(exc))
        return
    verified_hashes = verify_fit_hashes(fit, report)

    configuration_paths = [
        path for path in fit
        if path.startswith("/configurations/") and path.count("/") == 2
    ]
    inner_blobs: list[tuple[str, bytes]] = []
    default_config = c_string(fit.get("/configurations", {}).get("default", b""))
    default_props = fit.get(f"/configurations/{default_config}", {})
    report.require(
        len(configuration_paths) == 1 and
        configuration_paths[0] == f"/configurations/{default_config}",
        "fit.configuration.selection",
        "FIT has one configuration and explicitly selects it as default",
        {"default": default_config, "configurations": configuration_paths},
    )
    kernel_name = c_string(default_props.get("kernel", b""))
    kernel_props = fit.get(f"/images/{kernel_name}", {})
    kernel_data = kernel_props.get("data", b"")
    kernel_identity = {
        key: c_string(kernel_props.get(key, b""))
        for key in ("description", "type", "arch", "os", "compression")
    }
    kernel_identity["load"] = kernel_props.get("load", b"").hex()
    kernel_identity["entry"] = kernel_props.get("entry", b"").hex()
    report.require(
        bool(kernel_name) and bool(kernel_data) and
        kernel_identity == {
            "description": "ARM64 OpenWrt Linux-6.12.94",
            "type": "kernel",
            "arch": "arm64",
            "os": "linux",
            "compression": "lzma",
            "load": struct.pack(">Q", 0x48000000).hex(),
            "entry": struct.pack(">Q", 0x48000000).hex(),
        },
        "fit.kernel.identity",
        "default FIT configuration selects the pinned ARM64 Linux 6.12.94 LZMA payload",
        {"name": kernel_name, **kernel_identity},
    )
    kernel_path = f"/images/{kernel_name}"
    kernel_hashes = verified_hashes.get(kernel_path, [])
    report.require(
        bool(kernel_name) and
        {item["algorithm"] for item in kernel_hashes} == {"crc32", "sha1"} and
        len(kernel_hashes) == 2,
        "fit.kernel.selected_hash",
        "selected kernel has exactly the direct crc32 and sha1 hashes used by the locked FIT",
        {"image": kernel_path,
         "verified_hash_nodes": kernel_hashes},
    )
    decompressed_kernel = b""
    if kernel_data and kernel_identity.get("compression") == "lzma":
        try:
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
            decompressed_kernel = decoder.decompress(kernel_data, max_length=64 * 1024 * 1024)
            if not decoder.eof:
                raise VerificationError("kernel LZMA stream exceeds 64 MiB or is incomplete")
            if decoder.unused_data:
                raise VerificationError("kernel LZMA stream has trailing data")
            if not decompressed_kernel:
                raise VerificationError("kernel LZMA stream decompresses to an empty payload")
            report.passed(
                "fit.kernel.decompress",
                "selected kernel is one complete non-empty LZMA stream with no trailing data",
                {"compressed_bytes": len(kernel_data),
                 "decompressed_bytes": len(decompressed_kernel)},
            )
        except (lzma.LZMAError, EOFError, VerificationError) as exc:
            report.failed("fit.kernel.decompress", str(exc))
    else:
        report.failed(
            "fit.kernel.decompress",
            "selected kernel is absent or is not declared as LZMA",
            {"kernel_bytes": len(kernel_data),
             "compression": kernel_identity.get("compression")},
        )
    version_strings = sorted(set(
        match.decode("ascii", "replace")
        for match in re.findall(rb"Linux version [^\x00\r\n]{1,240}", decompressed_kernel)
    ))
    report.require(
        bool(decompressed_kernel) and len(decompressed_kernel) >= 64 and
        decompressed_kernel[56:60] == b"ARMd",
        "fit.kernel.arm64_header",
        "decompressed payload is non-empty and has the ARM64 Image magic",
        {"bytes": len(decompressed_kernel),
         "magic": decompressed_kernel[56:60].hex()},
    )
    report.require(
        bool(decompressed_kernel) and bool(version_strings) and
        all(value.startswith("Linux version 6.12.94 ") and
            "(builder@buildhost)" in value for value in version_strings),
        "fit.kernel.release_string",
        "embedded kernel version strings are Linux 6.12.94 with the pinned build identity",
        version_strings,
    )
    report.artifacts["fit_kernel"] = {
        "name": kernel_name,
        "compressed_bytes": len(kernel_data),
        "compressed_sha256": sha256_bytes(kernel_data),
        "decompressed_bytes": len(decompressed_kernel),
        "decompressed_sha256": sha256_bytes(decompressed_kernel),
        "version_strings": version_strings,
    }

    fdt_name = c_string(default_props.get("fdt", b""))
    fdt_path = f"/images/{fdt_name}"
    fdt_props = fit.get(fdt_path, {})
    fdt_data = fdt_props.get("data", b"")
    fdt_identity = {
        key: c_string(fdt_props.get(key, b""))
        for key in ("description", "type", "arch", "compression")
    }
    selected_fdt_valid = bool(fdt_name and fdt_data and fdt_identity == {
        "description": "ARM64 OpenWrt xiaomi_mi-router-ax3000t device tree blob",
        "type": "flat_dt",
        "arch": "arm64",
        "compression": "none",
    })
    if selected_fdt_valid:
        inner_blobs.append((fdt_name, fdt_data))
    report.require(
        selected_fdt_valid,
        "fit.dtb.selection",
        "the default FIT configuration explicitly selects one existing flat_dt payload",
        {"name": fdt_name,
         **fdt_identity,
         "bytes": len(fdt_data)},
    )
    fdt_hashes = verified_hashes.get(fdt_path, [])
    report.require(
        selected_fdt_valid and
        {item["algorithm"] for item in fdt_hashes} == {"crc32", "sha1"} and
        len(fdt_hashes) == 2,
        "fit.dtb.selected_hash",
        "selected DTB has exactly the direct crc32 and sha1 hashes used by the locked FIT",
        {"image": fdt_path,
         "verified_hash_nodes": fdt_hashes},
    )
    if not inner_blobs:
        return

    name, dtb = inner_blobs[0]
    try:
        nodes = parse_fdt(dtb)
    except VerificationError as exc:
        report.failed("dtb.parse", str(exc))
        return
    root_compat = c_string(nodes.get("/", {}).get("compatible", b""))
    report.require(root_compat == EXPECTED_DEVICE, "dtb.compatible",
                   "embedded DTB is for Xiaomi AX3000T", root_compat)

    fixed_nodes = [
        path for path, props in nodes.items()
        if c_string(props.get("compatible", b"")) == "fixed-partitions"
    ]
    report.require(
        fixed_nodes == [EXPECTED_PARTITIONS_PATH],
        "dtb.fixed_partitions.path",
        "DTB has exactly one fixed-partitions table at the pinned SPI-NAND path",
        fixed_nodes,
    )
    spi_props = nodes.get("/soc/spi@1100a000", {})
    flash_props = nodes.get("/soc/spi@1100a000/flash@0", {})
    fixed_props = nodes.get(EXPECTED_PARTITIONS_PATH, {})
    report.require(
        c_string(spi_props.get("compatible", b"")) == "mediatek,mt7981-spi-ipm" and
        c_string(spi_props.get("status", b"")) == "okay" and
        spi_props.get("#address-cells") == struct.pack(">I", 1) and
        spi_props.get("#size-cells") == struct.pack(">I", 0) and
        c_string(flash_props.get("compatible", b"")) == "spi-nand" and
        flash_props.get("reg") == struct.pack(">I", 0) and
        "mediatek,nmbm" in flash_props and
        flash_props.get("mediatek,bmt-max-ratio") == struct.pack(">I", 1) and
        flash_props.get("mediatek,bmt-max-reserved-blocks") == struct.pack(">I", 64) and
        flash_props.get("mediatek,bmt-mtd-overridden-oobsize") == struct.pack(">I", 64),
        "dtb.spi_nand.identity",
        "partition table belongs to the enabled pinned MT7981 SPI-NAND/NMBM device",
        {"spi_compatible": c_string(spi_props.get("compatible", b"")),
         "spi_status": c_string(spi_props.get("status", b"")),
         "flash_compatible": c_string(flash_props.get("compatible", b"")),
         "nmbm": "mediatek,nmbm" in flash_props},
    )
    report.require(
        c_string(fixed_props.get("compatible", b"")) == "fixed-partitions" and
        fixed_props.get("#address-cells") == struct.pack(">I", 1) and
        fixed_props.get("#size-cells") == struct.pack(">I", 1),
        "dtb.fixed_partitions.cells",
        "fixed partition reg values use one 32-bit address cell and one 32-bit size cell",
        {"address_cells": fixed_props.get("#address-cells", b"").hex(),
         "size_cells": fixed_props.get("#size-cells", b"").hex()},
    )
    partition_paths = [
        path for path in nodes
        if path.rsplit("/", 1)[-1].startswith("partition@")
    ]
    expected_paths = [
        f"{EXPECTED_PARTITIONS_PATH}/{entry[0]}" for entry in EXPECTED_PARTITIONS
    ]
    partitions: list[dict[str, Any]] = []
    for path in partition_paths:
        props = nodes[path]
        partitions.append({
            "path": path,
            "label": c_string(props.get("label", b"")),
            "reg": list(u32_pair(props.get("reg", b"")) or ()),
            "read_only": "read-only" in props,
        })
    report.require(
        partition_paths == expected_paths,
        "dtb.fixed_partitions.order",
        "DTB contains exactly the nine pinned partition nodes in compiled-DT order",
        partition_paths,
    )
    expected_partitions = [
        {
            "path": f"{EXPECTED_PARTITIONS_PATH}/{node}",
            "label": label,
            "reg": [offset, size],
            "read_only": read_only,
        }
        for node, label, offset, size, read_only in EXPECTED_PARTITIONS
    ]
    report.require(
        partitions == expected_partitions,
        "dtb.fixed_partitions.exact",
        "all nine partition labels, offsets, sizes, and read-only flags match the lock",
        {"actual": partitions, "expected": expected_partitions},
    )
    ubi = [p for p in partitions if p["path"].endswith("/partition@600000")]
    report.require(
        len(ubi) == 1 and ubi[0]["label"] == "ubi" and
        ubi[0]["reg"] == [EXPECTED_UBI_OFFSET, EXPECTED_UBI_SIZE],
        "dtb.single_ubi_112m",
        "partition@600000 is one 112 MiB partition named ubi",
        ubi,
    )
    kf = [p for p in partitions if p["path"].endswith("/partition@7600000")]
    report.require(
        len(kf) == 1 and kf[0]["label"] == "KF" and
        kf[0]["reg"] == [EXPECTED_KF_OFFSET, EXPECTED_KF_SIZE],
        "dtb.kf.boundary",
        "the 112 MiB UBI ends exactly at the preserved KF partition",
        kf,
    )
    labels = [p["label"] for p in partitions]
    report.require("ubi_kernel" not in labels, "dtb.no_ubi_kernel",
                   "DTB contains no separate ubi_kernel partition", partitions)
    report.require(
        not any(p["path"].endswith("/partition@2800000") for p in partitions),
        "dtb.no_stock_root_ubi",
        "DTB contains no stock-layout partition@2800000",
        partitions,
    )
    report.artifacts["embedded_dtb"] = {
        "fit_image": name,
        "bytes": len(dtb),
        "sha256": sha256_bytes(dtb),
        "partitions": partitions,
    }


def shell_function(text: str, name: str) -> str:
    match = re.search(rf"(?ms)^\s*{re.escape(name)}\s*\(\)\s*\{{(.*?)^\}}\s*$", text)
    return match.group(1) if match else ""


def verify_platform(path: Path, report: Report) -> None:
    if path.is_symlink() or not path.is_file():
        report.failed("upgrade.platform.read", "platform.sh is missing, non-regular, or a symlink")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.failed("upgrade.platform.read", f"cannot read platform.sh: {exc}")
        return
    actual_hash = sha256_file(path)
    report.require(
        actual_hash == EXPECTED_PLATFORM_SHA256,
        "upgrade.platform.exact_source",
        "final platform.sh is byte-identical to the locked post-patch generic-upgrade source",
        actual_hash,
    )
    do_upgrade = shell_function(text, "platform_do_upgrade")
    pre_upgrade = shell_function(text, "platform_pre_upgrade")
    exact_board_case = re.compile(
        rf"(?m)^\s*{re.escape(EXPECTED_DEVICE)}(?:\|\\|\))"
    )
    report.require(bool(do_upgrade), "upgrade.platform.function",
                   "platform_do_upgrade is present in the built rootfs")
    report.require(
        not exact_board_case.search(do_upgrade),
        "upgrade.generic_nand.path",
        "AX3000T has no special case and therefore reaches the default NAND path",
    )
    generic = re.search(r"(?ms)^\s*\*\)\s*\n\s*nand_do_upgrade\s+\"\$1\"", do_upgrade)
    report.require(bool(generic), "upgrade.generic_nand.default",
                   "the default platform_do_upgrade case calls nand_do_upgrade")
    report.require(
        not re.search(r"CI_KERN_UBIPART\s*=\s*['\"]?ubi_kernel", text),
        "upgrade.no_ubi_kernel_override",
        "built platform.sh never assigns CI_KERN_UBIPART=ubi_kernel",
    )
    report.require(
        not exact_board_case.search(pre_upgrade) and "xiaomi_initial_setup" not in pre_upgrade,
        "upgrade.no_layout_conversion",
        "AX3000T pre-upgrade path cannot rewrite the NAND layout",
    )
    report.artifacts["platform_sh"] = {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": actual_hash,
    }


def parse_manifest(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if " - " not in line:
            raise VerificationError(f"invalid package manifest line: {line!r}")
        name, version = line.split(" - ", 1)
        if name in packages:
            raise VerificationError(f"duplicate package in manifest: {name}")
        packages[name] = version
    return packages


def verify_package_manifest(path: Path, report: Report,
                            require_capture: bool = True) -> None:
    if not is_regular_file(path):
        report.failed("packages.manifest", "manifest is missing, non-regular, or a symlink")
        return
    try:
        packages = parse_manifest(path)
    except (OSError, UnicodeDecodeError, VerificationError) as exc:
        report.failed("packages.manifest", str(exc))
        return
    kernel = packages.get("kernel", "")
    mt7915e = packages.get("kmod-mt7915e", "")
    report.require(kernel == EXPECTED_KERNEL_PACKAGE_VERSION, "packages.kernel.release",
                   "package manifest records the exact Kwrt kernel package ABI", kernel)
    report.require(mt7915e == EXPECTED_MT7915E_PACKAGE_VERSION, "packages.mt76.revision",
                   "kmod-mt7915e has the exact pinned mt76 package version", mt7915e)
    report.require("kmod-mt76-connac" in packages and "kmod-mt76-core" in packages,
                   "packages.mt76.dependencies",
                   "manifest includes the mt76 core and connac packages")
    capture = packages.get("mtkcsi-dump", "")
    capture_ok = capture == EXPECTED_CAPTURE_PACKAGE_VERSION
    if require_capture:
        report.require(capture_ok, "packages.capture.pinned",
                       "manifest includes the pinned headless CSI capture package",
                       capture)
    elif capture_ok:
        report.passed("packages.capture.pinned",
                      "reference happens to include the pinned capture package", capture)
    else:
        report.warned("packages.capture.pinned",
                      "reference-only image predates the pinned capture package", capture)
    report.artifacts["package_manifest"] = {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "package_count": len(packages),
        "kernel": kernel,
        "kmod-mt7915e": mt7915e,
    }


def ascii_strings(data: bytes, minimum: int = 4) -> Iterable[str]:
    for match in re.finditer(rb"[\x20-\x7e]{%d,}" % minimum, data):
        yield match.group().decode("ascii", "replace")


def parse_elf_module(data: bytes) -> dict[str, Any]:
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise VerificationError("module is not a little-endian ELF64 object")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data, 0)
    machine = header[2]
    shoff, shentsize, shnum, shstrndx = header[6], header[11], header[12], header[13]
    if shentsize < 64 or shoff + shentsize * shnum > len(data) or shstrndx >= shnum:
        raise VerificationError("ELF section table is out of bounds")
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
        for i in range(shnum)
    ]

    def section_bytes(section: tuple[int, ...]) -> bytes:
        offset, size = section[4], section[5]
        if offset + size > len(data):
            raise VerificationError("ELF section content is out of bounds")
        return data[offset:offset + size]

    shstrings = section_bytes(sections[shstrndx])

    def read_name(table: bytes, offset: int) -> str:
        if offset >= len(table):
            raise VerificationError("ELF string offset is out of bounds")
        end = table.find(b"\0", offset)
        if end < 0:
            raise VerificationError("unterminated ELF string")
        return table[offset:end].decode("utf-8", "replace")

    names = [read_name(shstrings, section[0]) for section in sections]
    section_sizes = {name: section[5] for name, section in zip(names, sections)}
    undefined: set[str] = set()
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        if section[6] >= len(sections):
            raise VerificationError("ELF symbol string-table link is invalid")
        strings = section_bytes(sections[section[6]])
        entry_size = section[9] or 24
        if entry_size < 24 or section[5] % entry_size:
            raise VerificationError("ELF symbol table has an invalid entry size")
        for offset in range(section[4], section[4] + section[5], entry_size):
            name_offset, _info, _other, shndx, _value, _size = struct.unpack_from(
                "<IBBHQQ", data, offset
            )
            if shndx == 0 and name_offset:
                undefined.add(read_name(strings, name_offset))
    undefined_sorted = sorted(undefined)
    undefined_bytes = ("\n".join(undefined_sorted) + "\n").encode("utf-8")
    return {
        "machine": machine,
        "section_sizes": section_sizes,
        "undefined_symbols": undefined_sorted,
        "undefined_symbols_count": len(undefined_sorted),
        "undefined_symbols_sha256": hashlib.sha256(undefined_bytes).hexdigest(),
    }


def module_metadata(data: bytes) -> tuple[str, set[str], list[str]]:
    strings = list(ascii_strings(data))
    vermagic = next((s.split("=", 1)[1] for s in strings if s.startswith("vermagic=")), "")
    depends = next((s.split("=", 1)[1] for s in strings if s.startswith("depends=")), "")
    markers = [m for m in ("mt7915_mcu_set_csi", "mt7915_vendor_register",
                           "mt7915_vendor_csi_ctrl") if any(m in s for s in strings)]
    return vermagic, {item for item in depends.split(",") if item}, markers


def verify_module(path: Path, report: Report, baseline: bool = False) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        report.failed("module.read", "mt7915e module is missing, non-regular, or a symlink")
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        report.failed("module.read", f"cannot read mt7915e module: {exc}")
        return
    try:
        elf = parse_elf_module(data)
    except VerificationError as exc:
        report.failed("module.elf", str(exc))
        return None
    vermagic, actual_deps, markers = module_metadata(data)
    prefix = "module.baseline" if baseline else "module.patched"
    report.require(elf["machine"] == 183, prefix + ".architecture",
                   "module is ELF64 AArch64", elf["machine"])
    section_size = elf["section_sizes"].get(".gnu.linkonce.this_module")
    report.require(section_size == EXPECTED_THIS_MODULE_SIZE,
                   prefix + ".this_module_size",
                   "module layout matches the Kwrt 0x440-byte ABI, not the 0x280 SDK ABI",
                   hex(section_size) if section_size is not None else None)
    report.require(vermagic == EXPECTED_VERMAGIC, prefix + ".vermagic",
                   "mt7915e vermagic matches the pinned kernel ABI", vermagic)
    expected_deps = {"mt76-connac-lib", "mt76", "mac80211", "cfg80211"}
    report.require(actual_deps == expected_deps, prefix + ".dependencies",
                   "mt7915e dependency set matches the live/pinned baseline",
                   sorted(actual_deps))
    version_sections = sorted(
        name for name in elf["section_sizes"]
        if name.startswith("__version") or name == "__versions"
    )
    report.require(
        not version_sections and "modversions" not in vermagic,
        prefix + ".no_modversions",
        "audited Kwrt ABI has no __versions section and no modversions vermagic token",
        version_sections,
    )
    if baseline:
        report.require(
            len(data) == EXPECTED_BASELINE_MODULE_BYTES and
            sha256_bytes(data) == EXPECTED_BASELINE_MODULE_SHA256,
            "module.baseline.byte_identity",
            "vanilla module is byte-identical to the public/live Kwrt baseline",
            {"bytes": len(data), "sha256": sha256_bytes(data)},
        )
        report.require(
            elf["undefined_symbols_count"] == EXPECTED_BASELINE_UNDEFINED_COUNT and
            elf["undefined_symbols_sha256"] == EXPECTED_BASELINE_UNDEFINED_SHA256,
            "module.baseline.undefined_symbols",
            "vanilla control module matches the exact 294-symbol Kwrt ABI baseline",
            {"count": elf["undefined_symbols_count"],
             "sha256": elf["undefined_symbols_sha256"]},
        )
        report.require(not markers, "module.baseline.no_csi",
                       "vanilla control module contains no CSI implementation markers", markers)
    else:
        report.require(
            elf["undefined_symbols_count"] == EXPECTED_PATCHED_UNDEFINED_COUNT and
            elf["undefined_symbols_sha256"] == EXPECTED_PATCHED_UNDEFINED_SHA256,
            "module.patched.undefined_symbols",
            "patched module has the exact audited 297-symbol dependency set",
            {"count": elf["undefined_symbols_count"],
             "sha256": elf["undefined_symbols_sha256"]},
        )
        report.require(bool(markers), "module.patched.csi_present",
                       "built module contains CSI implementation markers", markers)
    artifact_name = "mt7915e_vanilla_module" if baseline else "mt7915e_module"
    report.artifacts[artifact_name] = {
        "path": path.name,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "vermagic": vermagic,
        "depends": sorted(actual_deps),
        "csi_markers": markers,
        "this_module_section_size": section_size,
        "undefined_symbols_count": elf["undefined_symbols_count"],
        "undefined_symbols_sha256": elf["undefined_symbols_sha256"],
    }
    elf["data"] = data
    return elf


def read_ipk(path: Path) -> tuple[dict[str, str], bytes]:
    if not is_regular_file(path):
        raise VerificationError("kernel package is missing, non-regular, or a symlink")
    if path.suffix != ".ipk":
        raise VerificationError("kernel package is not an .ipk (SDK .apk is forbidden)")
    try:
        with tarfile.open(path, "r:*") as outer:
            names = set(outer.getnames())
            control_name = next((n for n in names if n.lstrip("./") == "control.tar.gz"), None)
            data_name = next((n for n in names if n.lstrip("./") == "data.tar.gz"), None)
            if not control_name or not data_name:
                raise VerificationError("IPK lacks control.tar.gz or data.tar.gz")
            control_blob = outer.extractfile(control_name).read()  # type: ignore[union-attr]
            data_blob = outer.extractfile(data_name).read()  # type: ignore[union-attr]
        with tarfile.open(fileobj=io.BytesIO(control_blob), mode="r:*") as control_tar:
            control_member = next(
                (m for m in control_tar.getmembers() if m.name.lstrip("./") == "control"), None
            )
            if not control_member:
                raise VerificationError("IPK control file is missing")
            control_text = control_tar.extractfile(control_member).read().decode("utf-8")  # type: ignore[union-attr]
        fields: dict[str, str] = {}
        for line in control_text.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        with tarfile.open(fileobj=io.BytesIO(data_blob), mode="r:*") as data_tar:
            module_member = next(
                (m for m in data_tar.getmembers() if m.name.endswith("/mt7915e.ko")), None
            )
            if not module_member:
                raise VerificationError("IPK data payload lacks mt7915e.ko")
            module = data_tar.extractfile(module_member).read()  # type: ignore[union-attr]
        return fields, module
    except (OSError, tarfile.TarError, UnicodeDecodeError, AttributeError) as exc:
        raise VerificationError(f"invalid IPK: {exc}") from exc


def verify_kmod_package(path: Path, module_path: Path, report: Report,
                        baseline: bool = False) -> None:
    prefix = "kmod.baseline" if baseline else "kmod.patched"
    try:
        if not is_regular_file(module_path):
            raise VerificationError("loose module is missing, non-regular, or a symlink")
        fields, packaged_module = read_ipk(path)
        loose_module = module_path.read_bytes()
    except (VerificationError, OSError) as exc:
        report.failed(prefix + ".read", str(exc))
        return
    depends = fields.get("Depends", "")
    report.require(EXPECTED_KERNEL_DEPENDENCY in depends,
                   prefix + ".kernel_dependency",
                   "IPK depends on the exact Kwrt kernel package ABI",
                   depends)
    report.require(fields.get("Package") == "kmod-mt7915e",
                   prefix + ".identity", "package identity is kmod-mt7915e", fields)
    report.require(packaged_module == loose_module,
                   prefix + ".module_identity",
                   "IPK module is byte-identical to the separately audited module",
                   {"ipk_module_sha256": sha256_bytes(packaged_module),
                    "loose_module_sha256": sha256_bytes(loose_module)})
    report.artifacts["vanilla_kmod_ipk" if baseline else "kmod_ipk"] = {
        "path": path.name, "bytes": path.stat().st_size,
        "sha256": sha256_file(path), "control": fields,
    }


HIGH_RISK_PATHS = [
    re.compile(r"(^|/)(dropbear_.+_host_key|ssh_host_.+_key)$", re.I),
    re.compile(
        r"(^|/)(?:authorized_keys\d*|id_(?:rsa|ed25519|ecdsa)(?:\.pub)?|"
        r"ssh_host_.+_key(?:\.pub)?)$", re.I
    ),
    re.compile(r"(^|/)mtd\d+(?:[-_.][^/]*)?$", re.I),
    re.compile(
        r"(^|/)(?:factory|nvram|bdata|eeprom|calibration)(?:[-_.][^/]*)?\."
        r"(?:bin|img|dump|bak|backup|ubi|ubifs|tgz|tar|tar\.gz)$", re.I
    ),
    re.compile(r"(^|/)(?:sysupgrade\.tgz|backup[-_.][^/]+)$", re.I),
    re.compile(r"\.(?:ubi|ubifs|zip|7z|tgz|tar|tar\.gz)$", re.I),
]
PRIVATE_KEY_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----\r?\n"
    rb"(?:[A-Za-z0-9+/=]{1,96}\r?\n){2,}"
    rb"-----END \1-----"
)
MAC_RE = re.compile(r"(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
MAX_EVIDENCE_SAMPLES = 20
SENSITIVE_ROOT_PREFIXES = (
    "etc/config", "etc/init.d", "etc/opkg", "etc/apk", "etc/dropbear",
    "lib/modules", "lib/upgrade", "lib/wifi", "usr/sbin", "usr/share/doc",
)


def placeholder_mac(value: str) -> bool:
    return value.lower() in {
        "00:00:00:00:00:00",
        "ff:ff:ff:ff:ff:ff",
        "00:11:22:33:44:55",
        "02:00:00:00:00:00",
        "aa:bb:cc:dd:ee:ff",
    }


def scan_root(root: Path, report: Report) -> None:
    high_risk: list[str] = []
    secret_values: list[dict[str, Any]] = []
    macs: list[dict[str, Any]] = []
    unreadable: list[str] = []
    unsafe_symlinks: list[dict[str, str]] = []
    scanned = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError:
                unreadable.append(rel)
                continue
            identity_symlink = any(
                rel == prefix or rel.startswith(prefix + "/")
                for prefix in SENSITIVE_ROOT_PREFIXES
            ) or rel in {"etc/ethers", "etc/board.json", "etc/shadow", "etc/passwd"}
            if identity_symlink or any(
                rx.search(rel) or rx.search(target) for rx in HIGH_RISK_PATHS
            ):
                unsafe_symlinks.append({"path": rel, "target": target})
            continue
        if not path.is_file():
            continue
        scanned += 1
        if any(rx.search(rel) for rx in HIGH_RISK_PATHS):
            high_risk.append(rel)
        try:
            data = path.read_bytes()
        except OSError:
            unreadable.append(rel)
            continue
        if PRIVATE_KEY_BLOCK.search(data):
            high_risk.append(rel + ":embedded-private-key-block")

        is_uci = rel.startswith("etc/config/") and "/" not in rel[len("etc/config/"):]
        is_identity_text = is_uci or rel in {"etc/ethers", "etc/board.json"}
        if not is_identity_text:
            continue
        if b"\0" in data[:4096]:
            high_risk.append(rel + ":binary-identity-config")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            high_risk.append(rel + ":non-utf8-identity-config")
            continue

        if is_uci:
            for section in parse_uci_sections(text):
                for field, value in section["options"].items():
                    secret_field = field in {
                        "password", "passwd", "pppoe_username", "pppoe_password"
                    } or (rel == "etc/config/wireless" and field == "key")
                    if not secret_field or value == "":
                        continue
                    allowed = (
                        (rel, field, value) == ("etc/config/rpcd", "password", "$p$root") or
                        (rel, field, value) == ("etc/config/luci", "passwd", "/etc/passwd")
                    )
                    if not allowed:
                        secret_values.append({
                            "path": rel,
                            "section_type": section["type"],
                            "section_line": section["line"],
                            "field": field,
                            "value_length": len(value),
                        })

        uncommented = "\n".join(
            line.split("#", 1)[0] for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        for value in sorted(set(MAC_RE.findall(uncommented))):
            if not placeholder_mac(value):
                macs.append({"path": rel, "line_redacted": True})

    account_files = (
        ("etc/shadow", 9, {"", "!", "*"}),
        ("etc/passwd", 7, {"x", "!", "*"}),
    )
    for relative, field_count, allowed_password_fields in account_files:
        try:
            account_path = safe_root_path(root, relative)
        except VerificationError:
            high_risk.append(relative + ":symlink-ancestor")
            continue
        if not account_path.exists():
            # passwd/shadow presence is enforced elsewhere by base-files; the
            # privacy gate only validates every account if the file exists.
            continue
        if not account_path.is_file():
            high_risk.append(relative + ":non-regular")
            continue
        try:
            lines = account_path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeDecodeError):
            unreadable.append(relative)
            continue
        names: set[str] = set()
        for line_number, line in enumerate(lines, 1):
            fields = line.split(":")
            if len(fields) != field_count or not fields[0] or fields[0] in names:
                secret_values.append({
                    "path": relative,
                    "line": line_number,
                    "field": "malformed-or-duplicate-account",
                })
                continue
            names.add(fields[0])
            if fields[1] not in allowed_password_fields:
                secret_values.append({
                    "path": relative,
                    "line": line_number,
                    "account": fields[0],
                    "field": "password-or-hash",
                    "value_length": len(fields[1]),
                })

    report.require(not high_risk, "privacy.private_key_paths",
                   "rootfs contains no generated keys, device dumps, or embedded archives",
                   high_risk[:MAX_EVIDENCE_SAMPLES])
    report.require(not unsafe_symlinks, "privacy.sensitive_symlinks",
                   "symlink names and targets do not reference private-artifact paths",
                   unsafe_symlinks[:MAX_EVIDENCE_SAMPLES])
    report.require(not unreadable, "privacy.read_complete",
                   "every regular file and symlink was readable during the privacy scan",
                   unreadable[:MAX_EVIDENCE_SAMPLES])
    report.require(not secret_values, "privacy.credentials",
                   "rootfs contains no non-placeholder credentials or password hashes",
                   secret_values[:MAX_EVIDENCE_SAMPLES])
    report.require(not macs, "privacy.device_macs",
                   "identity-bearing rootfs configuration contains no device MAC addresses",
                   macs[:MAX_EVIDENCE_SAMPLES])
    report.artifacts["privacy_scan"] = {
        "root": "<extracted-final-rootfs>",
        "files_scanned": scanned,
        "high_risk_count": len(high_risk),
        "unsafe_symlink_count": len(unsafe_symlinks),
        "unreadable_count": len(unreadable),
        "credential_candidate_count": len(secret_values),
        "mac_candidate_count": len(macs),
    }


def parse_uci_sections(text: str) -> list[dict[str, Any]]:
    """Parse only UCI section/option syntax needed by the fail-closed Wi-Fi gate."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section_re = re.compile(
        r"^\s*config\s+(?:'([^']+)'|\"([^\"]+)\"|([^\s#]+))"
        r"(?:\s+(?:'([^']*)'|\"([^\"]*)\"|([^\s#]+)))?"
    )
    option_re = re.compile(
        r"^\s*option\s+(?:'([^']+)'|\"([^\"]+)\"|([^\s#]+))\s+"
        r"(?:'([^']*)'|\"([^\"]*)\"|([^\s#]+))"
    )
    for line_number, line in enumerate(text.splitlines(), 1):
        section = section_re.match(line)
        if section:
            values = section.groups()
            current = {
                "type": next(value for value in values[:3] if value is not None),
                "name": next((value for value in values[3:] if value is not None), ""),
                "line": line_number,
                "options": {},
            }
            sections.append(current)
            continue
        option = option_re.match(line)
        if option and current is not None:
            values = option.groups()
            key = next(value for value in values[:3] if value is not None)
            value = next(value for value in values[3:] if value is not None)
            current["options"][key] = value
    return sections


def verify_wireless_safe_default(root: Path, report: Report) -> None:
    try:
        generator = safe_root_path(root, "lib/wifi/mac80211.uc")
        wireless = safe_root_path(root, "etc/config/wireless")
    except VerificationError as exc:
        report.failed("rootfs.wifi.generator", str(exc))
        report.failed("rootfs.wifi.safe_default", str(exc))
        return
    generator_regular = generator.is_file() and not generator.is_symlink()
    generator_data = b""
    if generator_regular:
        try:
            generator_data = generator.read_bytes()
        except OSError:
            generator_regular = False
    generator_text = generator_data.decode("utf-8", "replace")
    generator_evidence = {
        "path": "lib/wifi/mac80211.uc",
        "sha256": sha256_bytes(generator_data) if generator_data else None,
        "default_without_board_defaults_is_disabled":
            EXPECTED_WIFI_DISABLED_EXPRESSION in generator_text,
        "default_key_is_empty": EXPECTED_WIFI_EMPTY_KEY_EXPRESSION in generator_text,
        "runtime_board_detection_executed": False,
    }
    report.require(
        generator_regular and
        generator_evidence["sha256"] == EXPECTED_WIFI_GENERATOR_SHA256 and
        generator_evidence["default_without_board_defaults_is_disabled"] and
        generator_evidence["default_key_is_empty"],
        "rootfs.wifi.generator",
        "pinned upstream generator is present; without board defaults it generates disabled radio and an empty key",
        generator_evidence,
    )

    if not wireless.exists():
        report.passed(
            "rootfs.wifi.safe_default",
            "image does not preseed a wireless UCI file or alter a preserved user configuration",
            {
                "wireless_config_present": False,
                "runtime_board_detection_executed": False,
                "note": "runtime board defaults require a separate, read-only deployment preflight",
            },
        )
        return
    report.failed(
        "rootfs.wifi.safe_default",
        "generic image must not preseed /etc/config/wireless; preserved user configuration is never modified",
        {"wireless_config_present": True, "path_type": "regular" if wireless.is_file() else "other"},
    )


def verify_package_feeds(root: Path, report: Report) -> None:
    expected_name = "openwrt-csi-lab_core"
    expected_repo = (
        "file:///nonexistent/ax3000t-112m-csi-packages/"
        "targets/mediatek/filogic/packages"
    )
    try:
        opkg_conf = safe_root_path(root, "etc/opkg.conf")
        opkg_dir = safe_root_path(root, "etc/opkg")
        apk_dir = safe_root_path(root, "etc/apk")
        apk_repo_dir = safe_root_path(root, "etc/apk/repositories.d")
        nonexistent_root = safe_root_path(root, "nonexistent")
    except VerificationError as exc:
        report.failed("rootfs.packages.no_remote_feed", str(exc))
        report.failed("rootfs.packages.signature_check", str(exc))
        return
    distfeeds = opkg_dir / "distfeeds.conf"
    candidates = [opkg_conf]
    candidates.extend(sorted(opkg_dir.glob("*.conf")) if opkg_dir.is_dir() else [])
    candidates.extend(sorted(apk_dir.glob("repositories*")) if apk_dir.is_dir() else [])
    candidates.extend(sorted(apk_repo_dir.glob("*")) if apk_repo_dir.is_dir() else [])
    seen: set[str] = set()
    active_lines: list[dict[str, str]] = []
    unreadable: list[str] = []
    for path in candidates:
        rel = path.relative_to(root).as_posix()
        if rel in seen or not path.exists():
            continue
        seen.add(rel)
        if path.is_symlink() or not path.is_file():
            unreadable.append(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(rel)
            continue
        for line in text.splitlines():
            active = line.split("#", 1)[0].strip()
            if active:
                active_lines.append({"path": rel, "line": active})

    feed_lines = [item for item in active_lines if item["line"].split(maxsplit=1)[0].startswith("src")]
    remote_lines = [
        item for item in active_lines
        if re.search(r"(?i)https?://", item["line"])
    ]
    parsed_feeds: list[dict[str, str]] = []
    wrong_feed_lines: list[dict[str, str]] = []
    for item in feed_lines:
        tokens = item["line"].split()
        if (len(tokens) != 3 or tokens[0] != "src/gz" or
                tokens[1] != expected_name or
                tokens[2] != expected_repo):
            wrong_feed_lines.append(item)
            continue
        parsed_feeds.append({"path": item["path"], "name": tokens[1], "url": tokens[2]})
    distfeeds_regular = distfeeds.is_file() and not distfeeds.is_symlink()
    opkg_regular = opkg_conf.is_file() and not opkg_conf.is_symlink()
    opkg_text = ""
    if opkg_regular:
        try:
            opkg_text = opkg_conf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            opkg_regular = False
    report.require(
        distfeeds_regular and len(feed_lines) == 1 and len(parsed_feeds) == 1 and
        not wrong_feed_lines and not remote_lines and not unreadable and
        not nonexistent_root.exists(),
        "rootfs.packages.no_remote_feed",
        "all active package feeds use the guaranteed-nonexistent local file URL; no remote Kwrt/OpenWrt feed is shipped",
        {
            "feed_count": len(feed_lines),
            "expected_name": expected_name,
            "expected_repo": expected_repo,
            "parsed_feeds": parsed_feeds,
            "local_repo_root_present": nonexistent_root.exists(),
            "wrong_feed_lines": wrong_feed_lines[:MAX_EVIDENCE_SAMPLES],
            "remote_lines": remote_lines[:MAX_EVIDENCE_SAMPLES],
            "unreadable": unreadable[:MAX_EVIDENCE_SAMPLES],
        },
    )
    report.require(
        opkg_regular and bool(re.search(r"(?m)^\s*option\s+check_signature(?:\s+1)?\s*$", opkg_text)),
        "rootfs.packages.signature_check",
        "IPK signature checking is enabled in the final rootfs",
    )


def verify_release_branding(root: Path, report: Report) -> None:
    try:
        paths = [
            safe_root_path(root, "etc/openwrt_release"),
            safe_root_path(root, "etc/device_info"),
            safe_root_path(root, "usr/lib/os-release"),
        ]
    except VerificationError as exc:
        report.failed("rootfs.release.neutral_branding", str(exc))
        report.failed("rootfs.release.no_kwrt_branding", str(exc))
        return
    contents: list[str] = []
    bad_files: list[str] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or path.is_symlink():
            bad_files.append(rel)
            continue
        try:
            contents.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            bad_files.append(rel)
    combined = "\n".join(contents)
    report.require(
        not bad_files and "OpenWrt CSI Lab" in combined and
        "https://github.com/howtion0/MtkCSIdump-csi" in combined,
        "rootfs.release.neutral_branding",
        "release metadata uses the neutral lab manufacturer and public source repository",
        {"invalid_files": bad_files, "manufacturer_present": "OpenWrt CSI Lab" in combined,
         "repository_present": "https://github.com/howtion0/MtkCSIdump-csi" in combined},
    )
    stale_files: list[str] = []
    stale_pattern = re.compile(rb"(?i)openwrt\.ai|kiddin(?:9)?")
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if stale_pattern.search(path.read_bytes()):
                stale_files.append(path.relative_to(root).as_posix())
        except OSError:
            stale_files.append(path.relative_to(root).as_posix() + ":unreadable")
    report.require(
        not stale_files,
        "rootfs.release.no_kwrt_branding",
        "rootfs contains no historical Kwrt vendor or repository branding",
        stale_files[:MAX_EVIDENCE_SAMPLES],
    )


def verify_capture_default_off(root: Path, report: Report) -> None:
    try:
        config = safe_root_path(root, "etc/config/mtkcsi")
        init = safe_root_path(root, "etc/init.d/mtkcsi-dump")
    except VerificationError as exc:
        report.failed("rootfs.capture.disabled", str(exc))
        return
    config_regular = is_regular_file(config)
    init_regular = is_regular_file(init)
    config_text = ""
    init_text = ""
    try:
        if config_regular:
            config_text = config.read_text(encoding="utf-8")
        if init_regular:
            init_text = init.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        config_regular = False
        init_regular = False
    sections = parse_uci_sections(config_text) if config_regular else []
    main_sections = [
        section for section in sections
        if section["type"] == "capture" and section["name"] == "main"
    ]
    effective_enabled = main_sections[0]["options"].get("enabled") \
        if len(main_sections) == 1 else None
    evidence = {
        "config_regular": config_regular,
        "config_sha256": sha256_file(config) if config_regular else None,
        "section_count": len(sections),
        "main_section_count": len(main_sections),
        "effective_enabled": effective_enabled,
        "init_regular": init_regular,
        "init_executable": init_regular and os.access(init, os.X_OK),
        "init_sha256": sha256_file(init) if init_regular else None,
    }
    report.require(
        config_regular and init_regular and
        evidence["config_sha256"] == EXPECTED_CAPTURE_CONFIG_SHA256 and
        evidence["init_sha256"] == EXPECTED_CAPTURE_INIT_SHA256 and
        len(sections) == 1 and len(main_sections) == 1 and effective_enabled == "0" and
        evidence["init_executable"] and
        "config_get_bool enabled main enabled 0" in init_text and
        bool(re.search(r"(?m)^\s*\[ \"\$enabled\" -eq 1 \] \|\| return 0\s*$", init_text)),
        "rootfs.capture.disabled",
        "exact locked UCI/init bytes contain one capture main section whose effective enabled value is 0",
        evidence,
    )


def verify_final_root_components(root: Path, module_path: Path | None,
                                 report: Report) -> None:
    try:
        root_module = safe_root_path(root, "lib/modules/6.12.94/mt7915e.ko")
        capture = safe_root_path(root, "usr/sbin/mtkcsi-dump")
        docs = [
            safe_root_path(root, "usr/share/doc/mtkcsi-dump/UDP_V2.md"),
            safe_root_path(root, "usr/share/doc/mtkcsi-dump/ABI.md"),
        ]
    except VerificationError as exc:
        report.failed("rootfs.safe_paths", str(exc))
        return
    root_module_regular = root_module.is_file() and not root_module.is_symlink()
    report.require(root_module_regular, "rootfs.module.present",
                   "final rootfs contains mt7915e.ko")
    if root_module_regular and module_path:
        try:
            root_data = root_module.read_bytes()
            loose_data = module_path.read_bytes()
            report.require(root_data == loose_data, "rootfs.module.identity",
                           "final rootfs module is byte-identical to the audited patched module",
                           {"rootfs_sha256": sha256_bytes(root_data),
                            "audited_sha256": sha256_bytes(loose_data)})
        except OSError as exc:
            report.failed("rootfs.module.identity", str(exc))

    capture_regular = capture.is_file() and not capture.is_symlink()
    report.require(capture_regular and os.access(capture, os.X_OK),
                   "rootfs.capture.binary",
                   "headless mtkcsi-dump executable is installed")
    if capture_regular:
        try:
            data = capture.read_bytes()
            required_markers = {
                b"CSI2": "udp-v2",
                b"CSI channel width disagrees with nl80211 interface state":
                    "nl80211-width-gate",
                b"Channel changed or became unreadable while dumping CSI":
                    "before-after-channel-gate",
                b"Channel changed between CSI polls": "cross-poll-radio-epoch-gate",
            }
            found = [label for marker, label in required_markers.items() if marker in data]
            report.require(
                len(found) == len(required_markers),
                "rootfs.capture.radio_epoch_gates",
                "capture binary binds CSI batches to audited nl80211 frequency/width epochs",
                {"required": list(required_markers.values()), "found": found},
            )
            report.artifacts["capture_binary"] = {
                "path": "/usr/sbin/mtkcsi-dump", "bytes": len(data),
                "sha256": sha256_bytes(data), "markers": found,
            }
        except OSError as exc:
            report.failed("rootfs.capture.read", str(exc))

    docs_regular = all(p.is_file() and not p.is_symlink() for p in docs)
    report.require(docs_regular, "rootfs.capture.docs",
                   "UDP v2 and driver ABI documents are included",
                   [str(p.relative_to(root)) for p in docs
                    if p.is_file() and not p.is_symlink()])
    if docs_regular:
        udp_doc = docs[0].read_text(errors="replace")
        report.require(
            all(term in udp_doc for term in (
                "CENTER_FREQ1", "FREQ_IS_PRIMARY", "TONE_MASKED_REORDERED",
            )),
            "rootfs.capture.channel_semantics_docs",
            "installed UDP v2 documentation defines frequency fallback and type-5 tone ordering",
        )

    verify_capture_default_off(root, report)

    verify_wireless_safe_default(root, report)
    verify_package_feeds(root, report)
    verify_release_branding(root, report)


def extract_root(root_payload: bytes, unsquashfs: str, report: Report) -> tempfile.TemporaryDirectory[str] | None:
    temp = tempfile.TemporaryDirectory(prefix="ax3000t-stage4-root-")
    temp_path = Path(temp.name)
    image_path = temp_path / "root.squashfs"
    root_path = temp_path / "rootfs"
    image_path.write_bytes(root_payload)
    try:
        proc = subprocess.run(
            [unsquashfs, "-no-progress", "-d", str(root_path), str(image_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.failed("rootfs.extract", f"unsquashfs failed: {exc}")
        temp.cleanup()
        return None
    if proc.returncode != 0:
        report.failed("rootfs.extract", "unsquashfs returned non-zero",
                      proc.stdout[-4000:])
        temp.cleanup()
        return None
    report.passed("rootfs.extract", "final root payload was extracted from the sysupgrade image")
    return temp


def verify_kernel_release_file(path: Path, report: Report) -> None:
    if not is_regular_file(path):
        report.failed("kernel.release.file", "kernel.release is missing, non-regular, or a symlink")
        return
    try:
        release = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        report.failed("kernel.release.file", f"cannot read kernel release: {exc}")
        return
    report.require(release == EXPECTED_KERNEL_RELEASE, "kernel.release.file",
                   "build tree kernel.release is exactly 6.12.94", release)
    report.artifacts["kernel_release_file"] = {
        "path": path.name, "value": release, "sha256": sha256_file(path)
    }


def verify_kernel_config(path: Path, report: Report) -> None:
    if path.is_symlink() or not path.is_file():
        report.failed("kernel.config", "kernel .config is missing, non-regular, or a symlink")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.failed("kernel.config", f"cannot read kernel .config: {exc}")
        return
    required = {"CONFIG_ARM64=y", "CONFIG_MODULES=y", "CONFIG_MODULE_UNLOAD=y"}
    present = set(text.splitlines())
    report.require(
        required <= present and "CONFIG_MODVERSIONS=y" not in present,
        "kernel.config.abi_flags",
        "final kernel config has the audited ARM64/module ABI flags and no MODVERSIONS",
        {"required_present": sorted(required & present),
         "modversions_enabled": "CONFIG_MODVERSIONS=y" in present},
    )
    report.artifacts["kernel_config"] = {
        "path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)
    }


def verify_module_symvers(path: Path, report: Report) -> None:
    if path.is_symlink() or not path.is_file():
        report.failed("kernel.module_symvers", "Module.symvers is missing, non-regular, or a symlink")
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        report.failed("kernel.module_symvers", f"cannot read Module.symvers: {exc}")
        return
    entries: dict[str, list[tuple[str, str, str]]] = {}
    malformed = 0
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 4 or not re.fullmatch(r"0x[0-9a-fA-F]{8}", fields[0]):
            malformed += 1
            continue
        entries.setdefault(fields[1], []).append((fields[0].lower(), fields[2], fields[3]))
    required = {
        name: entries.get(name, []) for name in sorted(EXPECTED_PATCHED_UNDEFINED_DELTA)
    }
    report.require(
        not malformed and all(
            value == [("0x00000000", "vmlinux", "EXPORT_SYMBOL")]
            for value in required.values()
        ),
        "kernel.module_symvers.csi_dependencies",
        "final kernel exports each of the three new CSI dependencies exactly once",
        {"entries": required, "malformed_lines": malformed},
    )
    report.artifacts["module_symvers"] = {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "entry_count": sum(len(value) for value in entries.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--platform-sh", type=Path,
                        help="platform.sh copied from the final extracted rootfs or staging root")
    parser.add_argument("--package-manifest", type=Path)
    parser.add_argument("--module", type=Path, help="built mt7915e.ko")
    parser.add_argument("--kmod-package", type=Path,
                        help="patched Kwrt-format kmod-mt7915e .ipk")
    parser.add_argument("--baseline-module", type=Path,
                        help="vanilla module built first with the same Kwrt configuration")
    parser.add_argument("--baseline-kmod-package", type=Path,
                        help="vanilla Kwrt-format kmod-mt7915e .ipk")
    parser.add_argument("--kernel-release-file", type=Path)
    parser.add_argument("--kernel-config", type=Path)
    parser.add_argument("--module-symvers", type=Path)
    parser.add_argument("--ucert", type=Path,
                        help="locked host ucert executable used for cryptographic verification")
    parser.add_argument("--usign", type=Path,
                        help="locked host usign executable used for fingerprint verification")
    parser.add_argument("--release-public-key", type=Path,
                        help="independently pinned Stage4 usign public key")
    parser.add_argument("--release-base-ucert", type=Path,
                        help="pinned time-bearing Stage4 base ucert")
    parser.add_argument("--source-lock", type=Path,
                        help="source-lock.json containing the signing hashes/fingerprint")
    parser.add_argument("--root-dir", type=Path,
                        help="reference-only external rootfs (forbidden for release candidates)")
    parser.add_argument("--unsquashfs", help="extract and scan the root payload with this binary")
    parser.add_argument("--output", type=Path, help="write the machine-readable report here")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="audit an older/reference image without build evidence; never release it")
    args = parser.parse_args()

    if args.output and args.output.is_symlink():
        print("error: refusing to overwrite a symlinked report path", file=sys.stderr)
        return 2
    report = Report(args.image)
    if not is_regular_file(args.image):
        print("error: image is missing, non-regular, or a symlink", file=sys.stderr)
        return 2
    try:
        image_bytes = args.image.read_bytes()
    except OSError as exc:
        print(f"error: cannot read image: {exc}", file=sys.stderr)
        return 2
    report.artifacts["image"] = {
        "bytes": len(image_bytes), "sha256": sha256_bytes(image_bytes)
    }

    try:
        payloads = read_sysupgrade(args.image, report)
    except VerificationError as exc:
        report.failed("sysupgrade.tar", str(exc))
        payloads = {}
    verify_metadata(image_bytes, report)
    if "kernel" in payloads:
        verify_dtb(payloads["kernel"], report)

    temp_root: tempfile.TemporaryDirectory[str] | None = None
    root_dir: Path | None = None
    if args.unsquashfs:
        if "root" not in payloads:
            report.failed("rootfs.extract", "sysupgrade root payload is unavailable")
        else:
            temp_root = extract_root(payloads["root"], args.unsquashfs, report)
            if temp_root:
                root_dir = Path(temp_root.name) / "rootfs"
    elif args.root_dir:
        if args.allow_incomplete:
            root_dir = args.root_dir
            report.warned(
                "rootfs.external_directory",
                "external root directory is reference-only and is not bound to the image payload",
            )
        else:
            report.failed(
                "rootfs.extract",
                "--root-dir cannot prove final image contents; candidate verification requires --unsquashfs",
            )
    elif not args.allow_incomplete:
        report.failed("rootfs.extract", "candidate verification requires --unsquashfs")
    if root_dir:
        if root_dir.is_symlink() or not root_dir.is_dir():
            report.failed("rootfs.directory", "rootfs directory is missing or a symlink")
            root_dir = None
        else:
            scan_root(root_dir, report)
            verify_final_root_components(root_dir, args.module, report)
            signing_args = (
                args.ucert, args.usign, args.release_public_key,
                args.release_base_ucert, args.source_lock,
            )
            if all(signing_args):
                verify_image_signature(
                    image_bytes, root_dir,
                    ucert=args.ucert,
                    usign=args.usign,
                    public_key=args.release_public_key,
                    base_ucert=args.release_base_ucert,
                    source_lock=args.source_lock,
                    report=report,
                )
            elif args.allow_incomplete:
                report.warned(
                    "signature.inputs",
                    "cryptographic signature evidence not supplied for reference-only audit",
                )
            else:
                report.failed(
                    "signature.inputs",
                    "--ucert, --usign, --release-public-key, --release-base-ucert, and --source-lock are required",
                )
    if root_dir:
        try:
            actual_platform = safe_root_path(root_dir, "lib/upgrade/platform.sh")
        except VerificationError as exc:
            report.failed("upgrade.platform.final", str(exc))
            actual_platform = None
        if actual_platform is None:
            pass
        elif is_regular_file(actual_platform):
            report.passed(
                "upgrade.platform.final",
                "platform.sh is a regular file extracted from the final root payload",
                "lib/upgrade/platform.sh",
            )
            verify_platform(actual_platform, report)
            if args.platform_sh:
                if not is_regular_file(args.platform_sh):
                    report.failed("upgrade.platform.provenance",
                                  "provided platform.sh is missing, non-regular, or a symlink")
                elif sha256_file(actual_platform) != sha256_file(args.platform_sh):
                    report.failed("upgrade.platform.provenance",
                                  "provided platform.sh differs from the final root payload")
                else:
                    report.passed(
                        "upgrade.platform.provenance",
                        "staging platform.sh is byte-identical to the final extracted rootfs file",
                        sha256_file(actual_platform),
                    )
        elif actual_platform.exists() or actual_platform.is_symlink():
            report.failed("upgrade.platform.final",
                          "final rootfs platform.sh is non-regular or a symlink")
        elif args.platform_sh:
            report.failed("upgrade.platform.final", "platform.sh is absent from extracted rootfs")
            if not is_regular_file(args.platform_sh):
                report.failed("upgrade.platform.provenance",
                              "provided staging platform.sh is missing, non-regular, or a symlink")
            else:
                staging_hash = sha256_file(args.platform_sh)
                report.require(
                    staging_hash == EXPECTED_PLATFORM_SHA256,
                    "upgrade.platform.provenance",
                    "staging platform.sh matches the source lock but cannot replace the missing final file",
                    staging_hash,
                )
                report.artifacts["staging_platform_sh"] = {
                    "path": args.platform_sh.name,
                    "bytes": args.platform_sh.stat().st_size,
                    "sha256": staging_hash,
                }
        else:
            report.failed("upgrade.platform.final", "platform.sh is absent from extracted rootfs")
    elif args.platform_sh:
        verify_platform(args.platform_sh, report)
        if args.allow_incomplete:
            report.warned("upgrade.platform.final",
                          "final rootfs was not extracted; staging source cannot prove final inclusion")
        else:
            report.failed("upgrade.platform.final",
                          "final rootfs was not extracted; staging source cannot replace final inclusion")
    elif args.allow_incomplete:
        report.warned("upgrade.platform.final",
                      "not checked: provide --platform-sh or --unsquashfs")
    else:
        report.failed("upgrade.platform.final",
                      "not checked: provide --platform-sh or --unsquashfs")

    if args.package_manifest:
        verify_package_manifest(args.package_manifest, report,
                                require_capture=not args.allow_incomplete)
    elif args.allow_incomplete:
        report.warned("packages.manifest", "build evidence not supplied for reference-only audit")
    else:
        report.failed("packages.manifest", "required build evidence was not supplied")

    patched_elf = verify_module(args.module, report) if args.module else None
    baseline_elf = verify_module(args.baseline_module, report, baseline=True) \
        if args.baseline_module else None
    for path, gate_name in (
        (args.module, "module.evidence"),
        (args.baseline_module, "module.baseline.evidence"),
    ):
        if not path:
            if args.allow_incomplete:
                report.warned(gate_name, "build evidence not supplied for reference-only audit")
            else:
                report.failed(gate_name, "required build evidence was not supplied")
    if patched_elf and baseline_elf:
        baseline_symbols = set(baseline_elf["undefined_symbols"])
        patched_symbols = set(patched_elf["undefined_symbols"])
        added = patched_symbols - baseline_symbols
        removed = baseline_symbols - patched_symbols
        report.require(
            added == EXPECTED_PATCHED_UNDEFINED_DELTA and not removed,
            "module.patched.exact_symbol_delta",
            "CSI patch adds exactly __nla_parse, nla_put, and skb_trim and removes no dependency",
            {"added": sorted(added), "removed": sorted(removed)},
        )

    if args.kmod_package and args.module:
        verify_kmod_package(args.kmod_package, args.module, report)
    elif args.allow_incomplete:
        report.warned("kmod.patched.evidence", "patched IPK evidence not supplied")
    else:
        report.failed("kmod.patched.evidence", "--kmod-package and --module are required")
    if args.baseline_kmod_package and args.baseline_module:
        verify_kmod_package(args.baseline_kmod_package, args.baseline_module,
                            report, baseline=True)
    elif args.allow_incomplete:
        report.warned("kmod.baseline.evidence", "vanilla IPK evidence not supplied")
    else:
        report.failed("kmod.baseline.evidence",
                      "--baseline-kmod-package and --baseline-module are required")

    if args.kernel_release_file:
        verify_kernel_release_file(args.kernel_release_file, report)
    elif args.allow_incomplete:
        report.warned("kernel.release.file", "build evidence not supplied for reference-only audit")
    else:
        report.failed("kernel.release.file", "required build evidence was not supplied")

    if args.kernel_config:
        verify_kernel_config(args.kernel_config, report)
    elif args.allow_incomplete:
        report.warned("kernel.config", "build evidence not supplied for reference-only audit")
    else:
        report.failed("kernel.config", "--kernel-config is required")
    if args.module_symvers:
        verify_module_symvers(args.module_symvers, report)
    elif args.allow_incomplete:
        report.warned("kernel.module_symvers", "build evidence not supplied for reference-only audit")
    else:
        report.failed("kernel.module_symvers", "--module-symvers is required")

    if not root_dir:
        if args.allow_incomplete:
            report.warned("privacy.rootfs", "final rootfs privacy scan was not run")
        else:
            report.failed("privacy.rootfs", "provide --root-dir or --unsquashfs for privacy scan")

    data = report.as_json()
    output = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    if temp_root:
        temp_root.cleanup()
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
