#!/usr/bin/env python3
"""Verify every public source/patch input before an AX3000T image build."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DTS_REL = Path("target/linux/mediatek/dts/mt7981b-xiaomi-mi-router-ax3000t.dts")
PLATFORM_REL = Path("target/linux/mediatek/filogic/base-files/lib/upgrade/platform.sh")
MT76_MAKEFILE = Path("package/kernel/mt76/Makefile")
KERNEL_DEFAULTS = Path("include/kernel-defaults.mk")
PROFILE_REL = Path("target/linux/mediatek/image/filogic.mk")
TOOLS_MAKEFILE = Path("tools/Makefile")
TOOLS_LZ4_MAKEFILE = Path("tools/lz4/Makefile")
PREPARE_DOWNLOAD_TARGETS = (
    "tools/lz4/download",
    "package/kernel/bpf-headers/download",
    "package/libs/argp-standalone/download",
    "package/libs/libmd/download",
    "package/libs/libpcap/download",
    "package/libs/libselinux/download",
    "package/libs/libsepol/download",
    "package/libs/ncurses/download",
    "package/libs/pcre2/download",
    "package/network/utils/resolveip/download",
    "package/system/ucert/download",
    "package/utils/lua/download",
    "package/utils/util-linux/download",
)
EXPECTED_DOWNLOAD_CLOSURE = {
    "schema": 1,
    "directories": 1,
    "files": 132,
    "manifest_sha256": "fd5f9a233c2313a5e4f5f7391aeb7be5f35b67537b657c30d861c4adb26c345c",
}
EXPECTED_SERIAL_PACKAGE_PREREQUISITES = [
    {
        "target": "package/utils/lua/compile",
        "jobs": 1,
        "reason": (
            "Lua 5.1 package compilation can leave zero-byte objects under "
            "inherited parallelism"
        ),
    },
]
EXPECTED_VANILLA_MT76_COMPILE = {
    "target": "package/kernel/mt76/compile",
    "jobs": 1,
    "reason": (
        "The direct ABI-control goal expands selected network dependencies that race under "
        "parallel package compilation"
    ),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    # Git's porcelain status uses both columns from byte zero.  In particular,
    # an unstaged worktree modification starts with a significant leading
    # space (" M path").  Strip only line terminators so callers comparing
    # porcelain output cannot accidentally turn it into the staged form
    # ("M  path").
    return proc.stdout.rstrip("\r\n")


def check(report: list[dict[str, Any]], name: str, condition: bool,
          expected: Any, actual: Any) -> None:
    report.append({
        "name": name,
        "status": "pass" if condition else "fail",
        "expected": expected,
        "actual": actual,
    })


def exact_board_case(function: str) -> bool:
    return bool(re.search(r"(?m)^\s*xiaomi,mi-router-ax3000t(?:\|\\|\))", function))


def shell_function(text: str, name: str) -> str:
    m = re.search(rf"(?ms)^\s*{re.escape(name)}\s*\(\)\s*\{{(.*?)^\}}\s*$", text)
    return m.group(1) if m else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--patch-dir", required=True, type=Path)
    parser.add_argument("--openwrt", required=True, type=Path)
    parser.add_argument("--kwrt", type=Path)
    parser.add_argument("--mt76", type=Path)
    parser.add_argument("--phase", choices=("pristine", "patched"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    unsafe_arguments = [
        str(path) for path in (args.lock, args.patch_dir, args.openwrt, args.kwrt, args.mt76)
        if path is not None and path.is_symlink()
    ]
    if unsafe_arguments or (args.output and args.output.is_symlink()):
        print(f"refusing symlinked source/output arguments: {unsafe_arguments}", file=sys.stderr)
        return 2

    # `git -C` changes the child process working directory.  Resolve caller-
    # supplied paths once so the clean-apply gate cannot accidentally test a
    # nonexistent repo-relative patch when this verifier is run from elsewhere.
    args.lock = args.lock.resolve()
    args.patch_dir = args.patch_dir.resolve()
    args.openwrt = args.openwrt.resolve()
    if args.kwrt:
        args.kwrt = args.kwrt.resolve()
    if args.mt76:
        args.mt76 = args.mt76.resolve()

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    gates: list[dict[str, Any]] = []

    lock_hash = sha256(args.lock)
    overlay = args.patch_dir.parent / lock["build_overlay"]["file"]
    overlay_hash = sha256(overlay) if overlay.is_file() else None
    check(gates, "build.overlay.sha256",
          overlay_hash == lock["build_overlay"]["sha256"],
          lock["build_overlay"]["sha256"], overlay_hash)
    overlay_text = read_text(overlay) if overlay.is_file() and not overlay.is_symlink() else ""
    for line in (
        "CONFIG_SIGNED_PACKAGES=y",
        "CONFIG_SIGNATURE_CHECK=y",
        "CONFIG_PER_FEED_REPO=n",
        'CONFIG_KERNEL_BUILD_USER="builder"',
        'CONFIG_KERNEL_BUILD_DOMAIN="buildhost"',
        'CONFIG_VERSION_DIST="OpenWrt-CSI-Lab"',
        'CONFIG_VERSION_NUMBER="25.12.5-experimental"',
        'CONFIG_VERSION_CODE="ax3000t-single-ubi-112m-csi"',
        'CONFIG_VERSION_REPO="file:///nonexistent/ax3000t-112m-csi-packages"',
        'CONFIG_VERSION_MANUFACTURER="OpenWrt CSI Lab"',
        'CONFIG_VERSION_HOME_URL="https://github.com/howtion0/MtkCSIdump-csi"',
        'CONFIG_VERSION_MANUFACTURER_URL="https://github.com/howtion0/MtkCSIdump-csi"',
    ):
        check(gates, f"build.overlay.safe_packages.{line.split('=', 1)[0]}",
              line in overlay_text, line, "present" if line in overlay_text else None)
    check(gates, "build.overlay.no_third_party_repo",
          "dl.openwrt.ai" not in overlay_text and "openwrt.ai" not in overlay_text and
          "Kiddin" not in overlay_text,
          "no historical Kwrt repository or branding",
          "none" if not re.search(r"(?i)openwrt\.ai|kiddin", overlay_text) else "present")
    builder = lock["builder"]
    dockerfile = args.patch_dir.parent / builder["dockerfile"]
    dockerfile_hash = sha256(dockerfile) if dockerfile.is_file() and not dockerfile.is_symlink() else None
    dockerfile_text = read_text(dockerfile)
    check(gates, "builder.dockerfile.sha256",
          dockerfile_hash == builder["dockerfile_sha256"],
          builder["dockerfile_sha256"], dockerfile_hash)
    from_lines = re.findall(r"(?m)^FROM\s+(\S+)\s*$", dockerfile_text)
    check(gates, "builder.base_digest",
          from_lines == [builder["base_digest"]],
          [builder["base_digest"]], from_lines)
    epoch_line = f'ARG SOURCE_DATE_EPOCH={builder["source_date_epoch"]}'
    check(gates, "builder.source_date_epoch",
          dockerfile_text.splitlines()[:1] == [epoch_line], epoch_line,
          dockerfile_text.splitlines()[0] if dockerfile_text else None)
    apt_sources = args.patch_dir.parent / builder["apt_sources_file"]
    apt_sources_hash = sha256(apt_sources) if is_regular_file(apt_sources) else None
    check(gates, "builder.apt_sources.sha256",
          apt_sources_hash == builder["apt_sources_sha256"],
          builder["apt_sources_sha256"], apt_sources_hash)
    apt_snapshot = builder["apt_snapshot"]
    snapshot_uri = f"https://snapshot.ubuntu.com/ubuntu/{apt_snapshot}"
    keyring_file = builder["apt_archive_keyring_file"]
    source_options = f"check-valid-until=no signed-by={keyring_file}"
    expected_apt_lines = [
        f"deb [{source_options}] {snapshot_uri} jammy main restricted universe multiverse",
        f"deb [{source_options}] {snapshot_uri} jammy-updates main restricted universe multiverse",
        f"deb [{source_options}] {snapshot_uri} jammy-backports main restricted universe multiverse",
        f"deb [{source_options}] {snapshot_uri} jammy-security main restricted universe multiverse",
    ]
    actual_apt_lines = read_text(apt_sources).splitlines()
    check(gates, "builder.apt_snapshot",
          re.fullmatch(r"20\d{6}T\d{6}Z", str(apt_snapshot)) is not None and
          builder.get("apt_snapshot_uri") == snapshot_uri and
          actual_apt_lines == expected_apt_lines,
          expected_apt_lines, actual_apt_lines)
    check(gates, "builder.apt_snapshot.direct_uri",
          bool(actual_apt_lines) and all(snapshot_uri in line for line in actual_apt_lines) and
          not any("archive.ubuntu.com" in line or "security.ubuntu.com" in line
                  for line in actual_apt_lines),
          snapshot_uri, actual_apt_lines)
    keyring_hash = builder.get("apt_archive_keyring_sha256")
    keyring_check = f'{keyring_hash}  {keyring_file}'
    check(gates, "builder.apt_archive_keyring.lock",
          keyring_file == "/usr/share/keyrings/ubuntu-archive-keyring.gpg" and
          re.fullmatch(r"[0-9a-f]{64}", str(keyring_hash)) is not None and
          keyring_check in dockerfile_text and
          all(f"signed-by={keyring_file}" in line for line in actual_apt_lines),
          {"path": keyring_file, "sha256": keyring_hash},
          {"docker_runtime_hash_check": keyring_check in dockerfile_text,
           "signed_by_on_every_source": bool(actual_apt_lines) and
           all(f"signed-by={keyring_file}" in line for line in actual_apt_lines)})
    ca_bootstrap = builder.get("ca_bootstrap", {})
    ca_url = str(ca_bootstrap.get("url") or "")
    ca_sha256 = str(ca_bootstrap.get("sha256") or "")
    ca_sha512 = str(ca_bootstrap.get("sha512") or "")
    ca_bytes = ca_bootstrap.get("bytes")
    ca_count = ca_bootstrap.get("certificate_count")
    ca_bundle_sha256 = str(ca_bootstrap.get("bundle_sha256") or "")
    ca_path = "/tmp/ca-certificates-bootstrap.deb"
    ca_add = f"ADD --checksum=sha256:{ca_sha256} \\\n    {ca_url} \\\n    {ca_path}"
    check(gates, "builder.ca_bootstrap.package_lock",
          ca_bootstrap.get("package") == "ca-certificates" and
          ca_bootstrap.get("version") == "20260601~22.04.1" and
          ca_url.startswith(f"{snapshot_uri}/pool/main/c/ca-certificates/") and
          re.fullmatch(r"[0-9a-f]{64}", ca_sha256) is not None and
          re.fullmatch(r"[0-9a-f]{128}", ca_sha512) is not None and
          ca_bytes == 140666 and ca_add in dockerfile_text and
          f'{ca_sha256}  {ca_path}' in dockerfile_text and
          f'{ca_sha512}  {ca_path}' in dockerfile_text and
          f'test "$(wc -c < {ca_path})" -eq {ca_bytes}' in dockerfile_text,
          {"package": "ca-certificates", "version": "20260601~22.04.1",
           "url_prefix": f"{snapshot_uri}/pool/main/c/ca-certificates/",
           "bytes": 140666, "sha256": ca_sha256, "sha512": ca_sha512},
          {"add_checksum": ca_add in dockerfile_text,
           "runtime_sha256": f'{ca_sha256}  {ca_path}' in dockerfile_text,
           "runtime_sha512": f'{ca_sha512}  {ca_path}' in dockerfile_text,
           "runtime_bytes": f'test "$(wc -c < {ca_path})" -eq {ca_bytes}' in
           dockerfile_text})
    check(gates, "builder.ca_bootstrap.bundle_lock",
          ca_count == 121 and
          re.fullmatch(r"[0-9a-f]{64}", ca_bundle_sha256) is not None and
          f"-eq {ca_count}" in dockerfile_text and
          f'{ca_bundle_sha256}  /etc/ssl/certs/ca-certificates.crt' in
          dockerfile_text,
          {"certificate_count": 121, "bundle_sha256": ca_bundle_sha256},
          {"count_check": f"-eq {ca_count}" in dockerfile_text,
           "bundle_hash_check":
           f'{ca_bundle_sha256}  /etc/ssl/certs/ca-certificates.crt' in
           dockerfile_text})
    check(gates, "builder.ca_bootstrap.tls_enforced",
          "http://snapshot.ubuntu.com" not in dockerfile_text and
          "Acquire::https::Verify-Peer" not in dockerfile_text and
          "Acquire::https::Verify-Host" not in dockerfile_text and
          "--no-check-certificate" not in dockerfile_text,
          "HTTPS snapshot with normal TLS peer/host verification",
          "no insecure TLS override" if "Verify-Peer" not in dockerfile_text else
          "insecure override present")
    check(gates, "builder.apt_sources.copy",
          "COPY apt-sources.list /etc/apt/sources.list" in dockerfile_text,
          "COPY apt-sources.list /etc/apt/sources.list",
          "present" if "COPY apt-sources.list /etc/apt/sources.list" in dockerfile_text else None)
    signing = lock.get("signing", {})
    stage_root = args.patch_dir.parent
    public_key = stage_root / str(signing.get("public_key_file", ""))
    base_ucert = stage_root / str(signing.get("base_ucert_file", ""))
    public_hash = sha256(public_key) if is_regular_file(public_key) else None
    cert_hash = sha256(base_ucert) if is_regular_file(base_ucert) else None
    check(gates, "signing.status.ready", signing.get("status") == "READY",
          "READY", signing.get("status"))
    check(gates, "signing.public_key.sha256",
          public_hash is not None and public_hash == signing.get("public_key_sha256"),
          signing.get("public_key_sha256"), public_hash)
    check(gates, "signing.base_ucert.sha256",
          cert_hash is not None and cert_hash == signing.get("base_ucert_sha256"),
          signing.get("base_ucert_sha256"), cert_hash)
    fingerprint = str(signing.get("usign_fingerprint") or "")
    check(gates, "signing.fingerprint.locked",
          re.fullmatch(r"[0-9a-f]{16,64}", fingerprint) is not None,
          "lowercase usign fingerprint", fingerprint or None)
    validfrom = signing.get("base_ucert_validfrom")
    expiresat = signing.get("base_ucert_expiresat")
    check(gates, "signing.base_ucert.validity_lock",
          isinstance(validfrom, int) and isinstance(expiresat, int) and
          validfrom == 1788126829 and expiresat == 1819662829 and
          expiresat > validfrom,
          {"validfrom": 1788126829, "expiresat": 1819662829},
          {"validfrom": validfrom, "expiresat": expiresat})
    private_in_tree = stage_root / "keys/ax3000t-stage4"
    forbidden_signing_material = sorted(
        path.relative_to(stage_root).as_posix()
        for path in (stage_root / "keys").glob("*")
        if path.name not in {"README.md", "ax3000t-stage4.pub", "ax3000t-stage4.ucert"}
    ) if (stage_root / "keys").is_dir() else []
    check(gates, "signing.private_key.absent",
          not private_in_tree.exists() and not private_in_tree.is_symlink() and
          not forbidden_signing_material,
          "private/revocation signing material absent; keys tree has exact public allowlist",
          {"private_key": "absent" if not private_in_tree.exists() and
           not private_in_tree.is_symlink() else "present",
           "unexpected_key_files": forbidden_signing_material})
    for name, expected in lock["patches"].items():
        path = args.patch_dir / name
        actual = sha256(path) if is_regular_file(path) else None
        check(gates, f"patch.sha256.{name}", actual == expected, expected, actual)

    for name, spec in lock.get("patch_application_normalization", {}).items():
        path = args.patch_dir / name
        raw = path.read_bytes() if is_regular_file(path) else b""
        normalized = raw + b"\n"
        check(gates, f"patch.normalization.{name}.historical_missing_lf",
              bool(raw) and not raw.endswith(b"\n") and
              spec["historical_file_ends_with_newline"] is False,
              "historical bytes end without LF", raw[-1:].hex() if raw else None)
        check(gates, f"patch.normalization.{name}.one_lf_only",
              normalized[:-1] == raw and normalized[-1:] == b"\n",
              "normalized[:-1] equals historical bytes; exactly one LF appended", None)
        hunk_count = len(re.findall(rb"(?m)^@@ ", raw))
        check(gates, f"patch.normalization.{name}.historical_hunk_count",
              hunk_count == spec["historical_hunk_count"],
              spec["historical_hunk_count"], hunk_count)
        actual_stream_hash = hashlib.sha256(normalized).hexdigest() if raw else None
        check(gates, f"patch.normalization.{name}.application_stream_sha256",
              actual_stream_hash == spec["application_stream_sha256"],
              spec["application_stream_sha256"], actual_stream_hash)

    capture_dir = args.patch_dir.parent / "package/mtkcsi-dump"
    expected_capture_entries = {
        "Makefile", "files", "files/mtkcsi.config", "files/mtkcsi-dump.init"
    }
    capture_entries = {
        path.relative_to(capture_dir).as_posix() for path in capture_dir.rglob("*")
    } if capture_dir.is_dir() and not capture_dir.is_symlink() else set()
    capture_symlinks = sorted(
        path.relative_to(capture_dir).as_posix()
        for path in capture_dir.rglob("*") if path.is_symlink()
    ) if capture_entries else []
    check(gates, "capture.package.exact_tree",
          capture_entries == expected_capture_entries and not capture_symlinks,
          sorted(expected_capture_entries),
          {"entries": sorted(capture_entries), "symlinks": capture_symlinks})
    for name, expected in lock["capture"]["package_files"].items():
        path = capture_dir / name
        actual = sha256(path) if is_regular_file(path) else None
        check(gates, f"capture.package.sha256.{name}", actual == expected,
              expected, actual)
    capture_make = (capture_dir / "Makefile").read_text(encoding="utf-8") \
        if is_regular_file(capture_dir / "Makefile") else ""
    check(gates, "capture.source.commit",
          lock["capture"]["commit"] in capture_make,
          lock["capture"]["commit"], "present" if lock["capture"]["commit"] in capture_make else None)
    check(gates, "capture.source.archive_hash",
          lock["capture"]["source_archive_sha256"] in capture_make,
          lock["capture"]["source_archive_sha256"],
          "present" if lock["capture"]["source_archive_sha256"] in capture_make else None)
    expected_capture_lines = (
        "PKG_SOURCE_PROTO:=git",
        "PKG_SOURCE_URL:=https://github.com/howtion0/MtkCSIdump-csi.git",
        f'PKG_SOURCE_VERSION:={lock["capture"]["commit"]}',
        "PKG_SOURCE_SUBMODULES:=skip",
        f'PKG_MIRROR_HASH:={lock["capture"]["source_archive_sha256"]}',
    )
    check(gates, "capture.source.git_protocol",
          all(line in capture_make for line in expected_capture_lines) and
          "codeload.github.com" not in capture_make and "PKG_HASH:=" not in capture_make,
          list(expected_capture_lines), "all present" if all(
              line in capture_make for line in expected_capture_lines) else "missing")
    capture_tree = lock["capture"].get("tree")
    capture_canonical = {
        "tree": capture_tree,
        "commit_timestamp": lock["capture"].get("commit_timestamp"),
        "format": lock["capture"].get("source_archive_format"),
        "bytes": lock["capture"].get("source_archive_bytes"),
        "sha256": lock["capture"].get("source_archive_sha256"),
        "member_count": lock["capture"].get("source_archive_member_count"),
        "members_sha256": lock["capture"].get("source_archive_members_sha256"),
    }
    check(gates, "capture.source.canonical_lock",
          re.fullmatch(r"[0-9a-f]{40}", str(capture_tree or "")) is not None and
          capture_canonical == {
              "tree": "9e54f6d5d1ac23ab8bc8ce18f6a40765d4e0417b",
              "commit_timestamp": 1788126290,
              "format": "openwrt-rawgit-normalized-tar.zst",
              "bytes": 14026970,
              "sha256": "6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18",
              "member_count": 109,
              "members_sha256": "49bab41ec3c541ec353acb9dc6df244d7724bf052e72fc3a56240f63c81d51f6",
          },
          "exact Stage3 commit-tree canonical archive identity", capture_canonical)
    archive_toolchain = lock["capture"].get("canonical_toolchain")
    expected_archive_toolchain = {
        "git": "2.34.1",
        "git_path": "/usr/bin/git",
        "gnu_tar": "1.34",
        "gnu_tar_path": "/usr/bin/tar",
        "preserve_git_archive_modes": True,
        "zstd": "1.4.8",
        "zstd_path": "/usr/bin/zstd",
        "zstd_threads": 1,
        "zstd_ultra_level": 20,
    }
    build_script = read_text(args.patch_dir.parent / "scripts/build_image.sh")
    archive_builder_tokens = (
        'CAPTURE_ARCHIVE_BYTES="14026970"',
        'CAPTURE_ARCHIVE_SHA256="6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18"',
        '"$(/usr/bin/git --version)" != "git version 2.34.1"',
        '"$(/usr/bin/tar --version | sed -n \'1p\')" != "tar (GNU tar) 1.34"',
        '"$(/usr/bin/zstd --version)" != *"v1.4.8"*',
        '/usr/bin/tar --same-permissions -C "$CAPTURE_TREE_DIR" -xf "$CAPTURE_GIT_TAR"',
        '/usr/bin/zstd -q -T1 --ultra -20 -c > "$CAPTURE_ARCHIVE_TMP"',
        'mv "$CAPTURE_ARCHIVE_TMP" "$CAPTURE_ARCHIVE"',
        '--zstd /usr/bin/zstd',
    )
    preseed_marker = 'mv "$CAPTURE_ARCHIVE_TMP" "$CAPTURE_ARCHIVE"'
    download_marker = '"${MAKE[@]}" -j"$JOBS" download'
    preseed_before_download = (
        preseed_marker in build_script and download_marker in build_script and
        build_script.index(preseed_marker) < build_script.index(download_marker)
    )
    check(gates, "capture.source.archive_builder_lock",
          archive_toolchain == expected_archive_toolchain and
          all(token in build_script for token in archive_builder_tokens) and
          preseed_before_download and
          build_script.count(
              'python3 "$STAGE_DIR/scripts/verify_capture_archive.py"') == 2,
          {"toolchain": expected_archive_toolchain,
           "preseed_before_download": True, "full_verifications": 2},
          {"toolchain": archive_toolchain,
           "tokens_present": all(token in build_script for token in archive_builder_tokens),
           "preseed_before_download": preseed_before_download,
           "full_verifications": build_script.count(
               'python3 "$STAGE_DIR/scripts/verify_capture_archive.py"')})

    prepare_array_matches = re.findall(
        r'(?ms)^PREPARE_DOWNLOAD_TARGETS=\(\n(.*?)^\)\n', build_script)
    expected_prepare_array_lines = [f'  "{target}"' for target in PREPARE_DOWNLOAD_TARGETS]
    actual_prepare_array_lines = (
        prepare_array_matches[0].splitlines() if len(prepare_array_matches) == 1 else []
    )
    prepare_download_loop = (
        'for prepare_download_target in "${PREPARE_DOWNLOAD_TARGETS[@]}"; do\n'
        "  printf '[networked-prepare] exact download target: %s\\n' \\\n"
        '    "$prepare_download_target" | tee -a "$LOG"\n'
        '  "${MAKE[@]}" -j1 "$prepare_download_target" 2>&1 | tee -a "$LOG"\n'
        "done"
    )
    general_download_marker = '"${MAKE[@]}" -j"$JOBS" download 2>&1 | tee -a "$LOG"'
    short_file_marker = 'if find "$OPENWRT/dl" -type f -size -1024c -print -quit'
    closure_create_marker = (
        'python3 "$STAGE_DIR/scripts/download_closure.py" create \\\n'
    )
    prepare_download_order = (
        build_script.count(general_download_marker) == 1 and
        build_script.count(prepare_download_loop) == 1 and
        build_script.count(short_file_marker) == 1 and
        build_script.count(closure_create_marker) == 1 and
        build_script.index(general_download_marker) <
        build_script.index(prepare_download_loop) <
        build_script.index(short_file_marker) <
        build_script.index(closure_create_marker)
    )
    locked_prepare_targets = builder.get("prepare_download_targets")
    check(gates, "builder.download_closure.prepare_targets",
          locked_prepare_targets == list(PREPARE_DOWNLOAD_TARGETS) and
          actual_prepare_array_lines == expected_prepare_array_lines and
          prepare_download_order,
          {"targets": list(PREPARE_DOWNLOAD_TARGETS), "execution": "serial -j1",
           "order": "general download -> exact targets -> short-file gate -> closure"},
          {"locked_targets": locked_prepare_targets,
           "script_targets": [line.strip().strip('"') for line in
                              actual_prepare_array_lines],
           "array_definitions": len(prepare_array_matches),
           "serial_loop_count": build_script.count(prepare_download_loop),
           "order_valid": prepare_download_order})

    expected_dependency_closure = {
        "method": "normalized-gnu-make-database",
        "tools_compile_direct_count": 53,
        "tools_compile_transitive_only": ["tools/lz4/compile"],
        "required_tool_download_targets": ["tools/lz4/download"],
        "host_download_aliases_rejected": [
            "package/libs/ncurses/host/download",
            "package/system/ucert/host/download",
        ],
        "canonical_host_source_targets": [
            "package/libs/ncurses/download",
            "package/system/ucert/download",
        ],
    }
    dependency_closure = builder.get("prepare_dependency_closure")
    tools_make_text = read_text(args.openwrt / TOOLS_MAKEFILE)
    tools_lz4_text = read_text(args.openwrt / TOOLS_LZ4_MAKEFILE)
    tools_dependency_witnesses = (
        "$(curdir)/builddirs-default := $(tools-y)",
        "$(curdir)/erofs-utils/compile := $(curdir)/libtool/compile "
        "$(curdir)/xz/compile $(curdir)/lz4/compile $(curdir)/util-linux/compile",
    )
    lz4_source_witnesses = (
        "PKG_VERSION:=1.10.0",
        "PKG_SOURCE_VERSION:=ebb370ca83af193212df4dcbadcc5d87bc0de2f0",
        "PKG_MIRROR_HASH:=b168683fbeee4182f6f64bc216ad23f3b94edefbca9b8792dcd99ecd0a49f20f",
    )
    rejected_host_aliases = expected_dependency_closure["host_download_aliases_rejected"]
    configuration_witnesses = (
        "grep -qx '# CONFIG_BUILD_ALL_HOST_TOOLS is not set' \"$OPENWRT/.config\"",
        "grep -qx '# CONFIG_TARGET_INITRAMFS_COMPRESSION_LZ4 is not set' "
        '"$OPENWRT/.config"',
    )
    dependency_closure_valid = (
        dependency_closure == expected_dependency_closure and
        all(token in tools_make_text for token in tools_dependency_witnesses) and
        all(token in tools_lz4_text for token in lz4_source_witnesses) and
        build_script.count("tools/lz4/download") == 1 and
        not any(alias in build_script for alias in rejected_host_aliases) and
        "CHECK_ALL" not in build_script and
        all(token in build_script for token in configuration_witnesses)
    )
    check(gates, "builder.prepare_dependency_closure",
          dependency_closure_valid,
          expected_dependency_closure,
          {"lock": dependency_closure,
           "tools_make_witnesses": all(
               token in tools_make_text for token in tools_dependency_witnesses),
           "lz4_source_witnesses": all(
               token in tools_lz4_text for token in lz4_source_witnesses),
           "lz4_download_occurrences": build_script.count("tools/lz4/download"),
           "rejected_host_alias_present": any(
               alias in build_script for alias in rejected_host_aliases),
           "check_all_present": "CHECK_ALL" in build_script,
           "disabled_config_witnesses": all(
               token in build_script for token in configuration_witnesses)})

    closure_verify_marker = (
        'python3 "$STAGE_DIR/scripts/download_closure.py" verify \\\n'
    )
    closure_verify_block = (
        'python3 "$STAGE_DIR/scripts/download_closure.py" verify \\\n'
        '  --root "$OPENWRT/dl" --manifest "$WORK_DIR/download-closure.json" \\\n'
        '  --lock "$STAGE_DIR/source-lock.json" | tee -a "$LOG"'
    )
    receipt_create_marker = 'python3 - "$WORK_DIR/.stage4-prepared.json"'
    receipt_validate_marker = 'python3 - "$PREPARED_MARKER"'
    receipt_create_count = build_script.count(receipt_create_marker)
    receipt_validate_count = build_script.count(receipt_validate_marker)
    receipt_create_position = (
        build_script.index(receipt_create_marker) if receipt_create_count == 1 else -1
    )
    receipt_validate_position = (
        build_script.index(receipt_validate_marker) if receipt_validate_count == 1 else -1
    )
    locked_download_closure = builder.get("download_closure")
    receipt_lock_witnesses = (
        'locked_download_manifest_sha256 = json.loads(',
        ')["builder"]["download_closure"]["manifest_sha256"]',
        'if download_manifest_sha256 != locked_download_manifest_sha256:',
        'raise SystemExit("download manifest differs from the locked closure before '
        'receipt creation")',
    )
    offline_prepare_marker = '"${MAKE[@]}" -j"$JOBS" prepare 2>&1 | tee -a "$LOG"'
    usign_compile_marker = (
        '"${MAKE[@]}" -j"$JOBS" package/system/usign/host/compile '
        '2>&1 | tee -a "$LOG"'
    )
    ucert_compile_marker = (
        '"${MAKE[@]}" -j"$JOBS" package/system/ucert/host/compile '
        '2>&1 | tee -a "$LOG"'
    )
    lua_compile_marker = (
        '"${MAKE[@]}" -j1 package/utils/lua/compile 2>&1 | tee -a "$LOG"'
    )
    mt76_compile_marker = (
        '"${MAKE[@]}" -j1 package/kernel/mt76/compile 2>&1 | tee -a "$LOG"'
    )
    vanilla_verify_marker = 'python3 "$STAGE_DIR/scripts/verify_vanilla_abi.py" \\\n'
    csi_patch_marker = 'cp "$STAGE_DIR/patches/999-mt7915-csi-v2-hardened.patch" \\\n'
    closure_verify_positions = [
        match.start() for match in re.finditer(re.escape(closure_verify_marker), build_script)
    ]
    closure_identity_valid = (
        locked_download_closure == EXPECTED_DOWNLOAD_CLOSURE and
        build_script.count(closure_verify_block) == 3 and
        len(closure_verify_positions) == 3 and
        receipt_create_count == 1 and receipt_validate_count == 1 and
        all(token in build_script for token in receipt_lock_witnesses) and
        build_script.index(closure_create_marker) < closure_verify_positions[0] <
        receipt_create_position
    )
    check(gates, "builder.download_closure.identity",
          closure_identity_valid,
          {**EXPECTED_DOWNLOAD_CLOSURE, "locked_verifications": 3,
           "receipt_cross_binding": True},
          {"lock": locked_download_closure,
           "locked_verify_blocks": build_script.count(closure_verify_block),
           "verify_markers": len(closure_verify_positions),
           "receipt_create_markers": receipt_create_count,
           "receipt_validate_markers": receipt_validate_count,
           "receipt_lock_witnesses": all(
               token in build_script for token in receipt_lock_witnesses),
           "networked_order_valid": (
               len(closure_verify_positions) == 3 and receipt_create_count == 1 and
               build_script.index(closure_create_marker) < closure_verify_positions[0] <
               receipt_create_position
           )})

    lua_validation_witnesses = (
        "verify_lua_serial_artifacts() {",
        "mapfile -t LUA_SOURCE_DIRS < <(find \"$OPENWRT/build_dir\" -type d",
        "[[ ${#LUA_SOURCE_DIRS[@]} -eq 1 ]] || {",
        "-path '*/lua-5.1.5/src' | sort)",
        "-name '*.o' -size 0",
        "Lua serial prerequisite left a zero-byte object",
        '[[ -s "${LUA_SOURCE_DIRS[0]}/liblua.so.5.1.5" ]] || {',
    )
    lua_pre_mt76_marker = 'verify_lua_serial_artifacts "before vanilla mt76"'
    lua_post_image_marker = 'verify_lua_serial_artifacts "after final image build"'
    final_image_build_marker = 'if ! "${MAKE[@]}" -j"$JOBS" 2>&1 | tee -a "$LOG"; then'
    target_output_marker = 'TARGET_OUT="$OPENWRT/bin/targets/mediatek/filogic"'
    lua_validation_order = (
        build_script.count(lua_pre_mt76_marker) == 1 and
        build_script.count(lua_post_image_marker) == 1 and
        build_script.count(final_image_build_marker) == 1 and
        build_script.count(target_output_marker) == 1 and
        build_script.index(lua_compile_marker) < build_script.index(lua_pre_mt76_marker) <
        build_script.index(mt76_compile_marker) < build_script.index(final_image_build_marker) <
        build_script.index(lua_post_image_marker) < build_script.index(target_output_marker)
    )
    lua_serial_valid = (
        builder.get("serial_package_prerequisites") ==
        EXPECTED_SERIAL_PACKAGE_PREREQUISITES and
        build_script.count(lua_compile_marker) == 1 and
        all(build_script.count(token) == 1 for token in lua_validation_witnesses) and
        lua_validation_order
    )
    check(gates, "builder.package_parallelism.lua",
          lua_serial_valid,
          {"prerequisites": EXPECTED_SERIAL_PACKAGE_PREREQUISITES,
           "unique_source_directory": True, "reject_zero_byte_objects": True,
           "nonempty_liblua": "liblua.so.5.1.5", "post_image_recheck": True},
          {"prerequisites": builder.get("serial_package_prerequisites"),
           "serial_compile_count": build_script.count(lua_compile_marker),
           "validation_witnesses": all(
               build_script.count(token) == 1 for token in lua_validation_witnesses),
           "validation_order": lua_validation_order})

    check(gates, "builder.package_parallelism.vanilla_mt76",
          builder.get("vanilla_mt76_compile") == EXPECTED_VANILLA_MT76_COMPILE and
          build_script.count(mt76_compile_marker) == 1 and
          '-j"$JOBS" package/kernel/mt76/compile' not in build_script,
          EXPECTED_VANILLA_MT76_COMPILE,
          {"lock": builder.get("vanilla_mt76_compile"),
           "serial_compile_count": build_script.count(mt76_compile_marker),
           "parallel_compile_present": (
               '-j"$JOBS" package/kernel/mt76/compile' in build_script)})

    unique_offline_markers = (
        offline_prepare_marker, usign_compile_marker, ucert_compile_marker,
        lua_compile_marker, lua_pre_mt76_marker, mt76_compile_marker,
        vanilla_verify_marker, csi_patch_marker,
    )
    offline_marker_counts = {
        marker: build_script.count(marker) for marker in unique_offline_markers
    }
    offline_prepare_order = (
        len(closure_verify_positions) == 3 and receipt_validate_count == 1 and
        all(count == 1 for count in offline_marker_counts.values()) and
        receipt_validate_position < closure_verify_positions[1] <
        build_script.index(offline_prepare_marker) < closure_verify_positions[2] <
        build_script.index(usign_compile_marker) < build_script.index(ucert_compile_marker) <
        build_script.index(lua_compile_marker) < build_script.index(lua_pre_mt76_marker) <
        build_script.index(mt76_compile_marker) <
        build_script.index(vanilla_verify_marker) < build_script.index(csi_patch_marker)
    )
    check(gates, "builder.offline.prepare_order",
          builder.get("offline_prepare_target") == "prepare" and
          offline_prepare_order and
          "tools/install" not in build_script and "toolchain/install" not in build_script,
          {"target": "prepare", "download_closure_verifications": 3,
           "order": ("network closure -> receipt; receipt validation -> closure -> prepare -> "
                     "closure -> usign -> ucert -> Lua -j1 -> mt76 -j1 -> ABI -> CSI")},
          {"locked_target": builder.get("offline_prepare_target"),
           "download_closure_verifications": len(closure_verify_positions),
           "unique_marker_counts": list(offline_marker_counts.values()),
           "manual_tools_install": "tools/install" in build_script,
           "manual_toolchain_install": "toolchain/install" in build_script,
           "order_valid": offline_prepare_order})

    try:
        openwrt_head = git(args.openwrt, "rev-parse", "HEAD")
        openwrt_tree = git(args.openwrt, "rev-parse", "HEAD^{tree}")
    except RuntimeError as exc:
        openwrt_head = openwrt_tree = f"error: {exc}"
    check(gates, "openwrt.commit",
          openwrt_head == lock["openwrt"]["commit"],
          lock["openwrt"]["commit"], openwrt_head)
    check(gates, "openwrt.pristine.tree",
          openwrt_tree == lock["openwrt"]["tree"],
          lock["openwrt"]["tree"], openwrt_tree)
    version_mk = read_text(args.openwrt / "include/version.mk")
    revision_match = re.search(r"(?m)^VERSION_CODE:=\$\(if \$\(VERSION_CODE\),\$\(VERSION_CODE\),(r\d+-[0-9a-f]+)\)$", version_mk)
    check(gates, "openwrt.revision.release_code",
          bool(revision_match and revision_match.group(1) == lock["openwrt"]["revision"]),
          lock["openwrt"]["revision"], revision_match.group(1) if revision_match else None)

    if args.kwrt:
        try:
            kwrt_head = git(args.kwrt, "rev-parse", "HEAD")
            kwrt_tree = git(args.kwrt, "rev-parse", "HEAD^{tree}")
        except RuntimeError as exc:
            kwrt_head = kwrt_tree = f"error: {exc}"
        check(gates, "kwrt.commit", kwrt_head == lock["kwrt_layout_source"]["commit"],
              lock["kwrt_layout_source"]["commit"], kwrt_head)
        check(gates, "kwrt.tree", kwrt_tree == lock["kwrt_layout_source"]["tree"],
              lock["kwrt_layout_source"]["tree"], kwrt_tree)
        try:
            kwrt_status = git(args.kwrt, "status", "--porcelain=v1", "--untracked-files=all")
        except RuntimeError as exc:
            kwrt_status = f"error: {exc}"
        check(gates, "kwrt.worktree.clean", kwrt_status == "",
              "no tracked or untracked Kwrt changes", kwrt_status or "clean")
        common_config = args.kwrt / "devices/common/.config"
        target_config = args.kwrt / "devices/mediatek_filogic/.config"
        common_diy = args.kwrt / "devices/common/diy.sh"
        common_hash = sha256(common_config) if common_config.is_file() else None
        target_hash = sha256(target_config) if target_config.is_file() else None
        check(gates, "kwrt.config.common.sha256",
              common_hash == lock["kwrt_layout_source"]["common_config_sha256"],
              lock["kwrt_layout_source"]["common_config_sha256"], common_hash)
        check(gates, "kwrt.config.target.sha256",
              target_hash == lock["kwrt_layout_source"]["target_config_sha256"],
              lock["kwrt_layout_source"]["target_config_sha256"], target_hash)
        common_text = common_config.read_text(encoding="utf-8") \
            if common_config.is_file() else ""
        check(gates, "kwrt.config.ipk_mode",
              bool(re.search(r"(?m)^CONFIG_USE_APK=n$", common_text)),
              "CONFIG_USE_APK=n", "present" if "CONFIG_USE_APK=n" in common_text else None)
        diy_hash = sha256(common_diy) if common_diy.is_file() else None
        check(gates, "kwrt.common_diy.sha256",
              diy_hash == lock["kwrt_layout_source"]["common_diy_sha256"],
              lock["kwrt_layout_source"]["common_diy_sha256"], diy_hash)
        diy_lines = common_diy.read_text(encoding="utf-8").splitlines() \
            if common_diy.is_file() else []
        transform = lock["kwrt_layout_source"]["vermagic_transform"]
        line_index = int(transform["source_line"]) - 1
        actual_line = diy_lines[line_index] if 0 <= line_index < len(diy_lines) else None
        check(gates, "kwrt.common_diy.vermagic_source_line",
              actual_line == transform["source_text"], transform["source_text"], actual_line)
        for name in ("23-ax3000t.patch", "25-platform.patch"):
            kwrt_patch = args.kwrt / "devices/mediatek_filogic/patches" / name
            actual = sha256(kwrt_patch) if kwrt_patch.is_file() else None
            check(gates, f"kwrt.patch.origin.{name}",
                  actual == lock["patches"][name], lock["patches"][name], actual)

    mt76_make = args.openwrt / MT76_MAKEFILE
    make_text = mt76_make.read_text(encoding="utf-8") if mt76_make.is_file() else ""
    make_rev = re.search(r"^PKG_SOURCE_VERSION:=(\w+)$", make_text, re.M)
    make_date = re.search(r"^PKG_SOURCE_DATE:=(\S+)$", make_text, re.M)
    make_release = re.search(r"^PKG_RELEASE[?:]?=(\d+)$", make_text, re.M)
    check(gates, "openwrt.mt76.commit",
          bool(make_rev and make_rev.group(1) == lock["mt76"]["commit"]),
          lock["mt76"]["commit"], make_rev.group(1) if make_rev else None)
    check(gates, "openwrt.mt76.source_date",
          bool(make_date and make_date.group(1) == lock["mt76"]["source_date"]),
          lock["mt76"]["source_date"], make_date.group(1) if make_date else None)
    check(gates, "openwrt.mt76.package_release",
          bool(make_release and int(make_release.group(1)) == lock["mt76"]["package_release"]),
          lock["mt76"]["package_release"], int(make_release.group(1)) if make_release else None)

    for tool in ("usign", "ucert"):
        tool_makefile = read_text(args.openwrt / f"package/system/{tool}/Makefile")
        commit_match = re.search(r"(?m)^PKG_SOURCE_VERSION:=(\w+)$", tool_makefile)
        hash_match = re.search(r"(?m)^PKG_MIRROR_HASH:=(\w+)$", tool_makefile)
        expected_commit = lock["signing_tools"][f"{tool}_commit"]
        expected_hash = lock["signing_tools"][f"{tool}_mirror_hash"]
        check(gates, f"signing_tools.{tool}.commit",
              bool(commit_match and commit_match.group(1) == expected_commit),
              expected_commit, commit_match.group(1) if commit_match else None)
        check(gates, f"signing_tools.{tool}.mirror_hash",
              bool(hash_match and hash_match.group(1) == expected_hash),
              expected_hash, hash_match.group(1) if hash_match else None)

    wifi_spec = lock["wifi_generator"]
    wifi_generator = args.openwrt / wifi_spec["source_path"]
    wifi_hash = sha256(wifi_generator) if wifi_generator.is_file() else None
    wifi_text = wifi_generator.read_text(encoding="utf-8") if wifi_generator.is_file() else ""
    check(gates, "wifi.generator.sha256", wifi_hash == wifi_spec["sha256"],
          wifi_spec["sha256"], wifi_hash)
    check(gates, "wifi.generator.default_disabled_expression",
          wifi_spec["default_disabled_expression"] in wifi_text,
          wifi_spec["default_disabled_expression"],
          "present" if wifi_spec["default_disabled_expression"] in wifi_text else None)
    check(gates, "wifi.generator.default_empty_key_expression",
          wifi_spec["default_key_expression"] in wifi_text,
          wifi_spec["default_key_expression"],
          "present" if wifi_spec["default_key_expression"] in wifi_text else None)

    if args.mt76:
        try:
            mt76_head = git(args.mt76, "rev-parse", "HEAD")
            mt76_tree = git(args.mt76, "rev-parse", "HEAD^{tree}")
            apply_proc = subprocess.run(
                ["git", "-C", str(args.mt76), "apply", "--check",
                 str(args.patch_dir / "999-mt7915-csi-v2-hardened.patch")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
            apply_result = apply_proc.stderr.strip() or apply_proc.stdout.strip()
        except RuntimeError as exc:
            mt76_head = mt76_tree = f"error: {exc}"
            apply_proc = None
            apply_result = str(exc)
        check(gates, "mt76.commit", mt76_head == lock["mt76"]["commit"],
              lock["mt76"]["commit"], mt76_head)
        check(gates, "mt76.tree", mt76_tree == lock["mt76"]["tree"],
              lock["mt76"]["tree"], mt76_tree)
        check(gates, "mt76.hardened_patch.applies",
              bool(apply_proc and apply_proc.returncode == 0), "clean apply", apply_result or "clean apply")

    dts = read_text(args.openwrt / DTS_REL)
    platform = read_text(args.openwrt / PLATFORM_REL)
    kernel_defaults = read_text(args.openwrt / KERNEL_DEFAULTS)
    profile = read_text(args.openwrt / PROFILE_REL)
    transform = lock["kwrt_layout_source"]["vermagic_transform"]
    has_upstream_vermagic = transform["upstream_text"] in kernel_defaults
    has_kwrt_vermagic = transform["patched_text"] in kernel_defaults
    do_upgrade = shell_function(platform, "platform_do_upgrade")
    pre_upgrade = shell_function(platform, "platform_pre_upgrade")
    has_single_ubi = bool(re.search(
        r"(?ms)partition@600000\s*\{.*?label\s*=\s*\"ubi\"\s*;.*?"
        r"reg\s*=\s*<0x0*600000\s+0x0*7000000>\s*;", dts
    ))
    has_ubi_kernel = 'label = "ubi_kernel"' in dts
    has_stock_ubi = "partition@2800000" in dts
    special_upgrade = exact_board_case(do_upgrade)
    special_pre = exact_board_case(pre_upgrade)
    installed_csi_patch = args.openwrt / MT76_MAKEFILE.parent / "patches/999-mt7915-csi-v2-hardened.patch"
    identity = lock["required_image_identity"]
    compat_version_line = f'  DEVICE_COMPAT_VERSION := {identity["compat_version"]}'
    compat_message_line = f'  DEVICE_COMPAT_MESSAGE := {identity["compat_message"]}'

    if args.phase == "pristine":
        try:
            worktree_status = git(
                args.openwrt, "status", "--porcelain=v1", "--untracked-files=all"
            )
        except RuntimeError as exc:
            worktree_status = f"error: {exc}"
        check(gates, "openwrt.worktree.pristine",
              worktree_status == "", "no tracked or untracked source changes",
              worktree_status or "clean")
        check(gates, "kwrt.vermagic.pristine",
              has_upstream_vermagic and not has_kwrt_vermagic,
              "upstream config hash before the one-line Kwrt ABI transform",
              {"upstream": has_upstream_vermagic, "kwrt_one": has_kwrt_vermagic})
        check(gates, "layout.pristine.stock", has_ubi_kernel and has_stock_ubi and not has_single_ubi,
              "stock dual-UBI source before patches",
              {"single_ubi": has_single_ubi, "ubi_kernel": has_ubi_kernel,
               "partition_2800000": has_stock_ubi})
        check(gates, "upgrade.pristine.special", special_upgrade and special_pre,
              "AX3000T stock layout special cases present",
              {"platform_do_upgrade": special_upgrade, "platform_pre_upgrade": special_pre})
        check(gates, "metadata.compat.pristine_stock",
              compat_version_line not in profile and compat_message_line not in profile,
              "stock profile has no single-UBI compatibility marker",
              {"version_marker": compat_version_line in profile,
               "message_marker": compat_message_line in profile})
    else:
        expected_changes = {
            " M include/kernel-defaults.mk",
            " M target/linux/mediatek/dts/mt7981b-xiaomi-mi-router-ax3000t.dts",
            " M target/linux/mediatek/filogic/base-files/lib/upgrade/platform.sh",
            " M target/linux/mediatek/image/filogic.mk",
            "?? package/kernel/mt76/patches/999-mt7915-csi-v2-hardened.patch",
            "?? package/utils/mtkcsi-dump/Makefile",
            "?? package/utils/mtkcsi-dump/files/mtkcsi-dump.init",
            "?? package/utils/mtkcsi-dump/files/mtkcsi.config",
        }
        try:
            worktree_status = set(filter(None, git(
                args.openwrt, "status", "--porcelain=v1", "--untracked-files=all"
            ).splitlines()))
        except RuntimeError as exc:
            worktree_status = {f"error: {exc}"}
        check(gates, "openwrt.worktree.patched_exact",
              worktree_status == expected_changes,
              sorted(expected_changes), sorted(worktree_status))
        for relative, expected in lock["post_patch_files"].items():
            path = args.openwrt / relative
            actual = sha256(path) if is_regular_file(path) else None
            check(gates, f"post_patch_file.sha256.{relative}", actual == expected,
                  expected, actual)
        check(gates, "kwrt.vermagic.patched",
              has_kwrt_vermagic and not has_upstream_vermagic,
              "exact historical Kwrt .vermagic=1 transform and no upstream hash command",
              {"upstream": has_upstream_vermagic, "kwrt_one": has_kwrt_vermagic})
        check(gates, "layout.patched.single_ubi", has_single_ubi and not has_ubi_kernel and not has_stock_ubi,
              "one ubi partition at 0x00600000/0x07000000",
              {"single_ubi": has_single_ubi, "ubi_kernel": has_ubi_kernel,
               "partition_2800000": has_stock_ubi})
        default_nand = bool(re.search(r"(?ms)^\s*\*\)\s*\n\s*nand_do_upgrade\s+\"\$1\"", do_upgrade))
        override = bool(re.search(r"CI_KERN_UBIPART\s*=\s*['\"]?ubi_kernel", platform))
        check(gates, "upgrade.patched.generic",
              not special_upgrade and not special_pre and default_nand and not override,
              "generic nand_do_upgrade, no layout conversion, no ubi_kernel override",
              {"special_upgrade": special_upgrade, "special_pre": special_pre,
               "default_nand": default_nand, "ubi_kernel_override": override})
        check(gates, "metadata.compat.patched",
              profile.count(compat_version_line) == 1 and profile.count(compat_message_line) == 1,
              "exact anti-misflash compat version/message appear once in AX3000T profile",
              {"version_count": profile.count(compat_version_line),
               "message_count": profile.count(compat_message_line)})
        installed_hash = sha256(installed_csi_patch) \
            if is_regular_file(installed_csi_patch) else None
        check(gates, "openwrt.mt76.hardened_patch.installed",
              installed_hash == lock["patches"]["999-mt7915-csi-v2-hardened.patch"],
              lock["patches"]["999-mt7915-csi-v2-hardened.patch"], installed_hash)

    result = {
        "schema": 1,
        "classification": "EXPERIMENTAL-DO-NOT-FLASH",
        "phase": args.phase,
        "result": "pass" if all(g["status"] == "pass" for g in gates) else "fail",
        "source_lock": args.lock.name,
        "source_lock_sha256": lock_hash,
        "gates": gates,
    }
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
