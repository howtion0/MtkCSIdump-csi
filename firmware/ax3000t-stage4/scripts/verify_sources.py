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
    return proc.stdout.strip()


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
