#!/usr/bin/env python3
"""Verify the OpenWrt-normalized Stage3 git source archive and Git tree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


EXPECTED_COMMIT = "b8d7b73fc582795e734086a676a0a18a15980cb8"
EXPECTED_TREE = "9e54f6d5d1ac23ab8bc8ce18f6a40765d4e0417b"
EXPECTED_ARCHIVE_BYTES = 14026970
EXPECTED_ARCHIVE_SHA256 = "6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18"
EXPECTED_MTIME = 1788126290
EXPECTED_PREFIX = "mtkcsi-dump-2.0.0~git20260830.b8d7b73"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_object_id(kind: str, body: bytes) -> bytes:
    header = kind.encode() + b" " + str(len(body)).encode() + b"\0"
    return hashlib.sha1(header + body).digest()  # noqa: S324 - Git object identity


def tree_id(files: list[tuple[tuple[str, ...], int, bytes]]) -> str:
    root: dict[str, Any] = {}
    for parts, mode, data in files:
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError("file/directory path collision")
            node = child
        if parts[-1] in node:
            raise ValueError("duplicate archive path")
        node[parts[-1]] = (mode, git_object_id("blob", data))

    def emit(node: dict[str, Any]) -> bytes:
        entries: list[tuple[bytes, bool, bytes]] = []
        for name, value in node.items():
            name_bytes = name.encode("utf-8")
            if isinstance(value, dict):
                oid = git_object_id("tree", emit(value))
                record = b"40000 " + name_bytes + b"\0" + oid
                entries.append((name_bytes, True, record))
            else:
                mode, oid = value
                record = f"{mode:o}".encode() + b" " + name_bytes + b"\0" + oid
                entries.append((name_bytes, False, record))
        entries.sort(key=lambda item: item[0] + (b"/" if item[1] else b""))
        return b"".join(item[2] for item in entries)

    return git_object_id("tree", emit(root)).hex()


def gate(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "expected": expected,
        "actual": actual,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--package-makefile", required=True, type=Path)
    parser.add_argument("--source-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--zstd", default="zstd", type=Path)
    args = parser.parse_args()
    gates: list[dict[str, Any]] = []
    try:
        if (not args.archive.is_file() or args.archive.is_symlink() or
                not args.package_makefile.is_file() or args.package_makefile.is_symlink() or
                not args.source_lock.is_file() or args.source_lock.is_symlink() or
                args.output.is_symlink()):
            raise ValueError("archive/Makefile/source-lock/output path is missing, non-regular, or symlinked")
        data = args.archive.read_bytes()
        makefile = args.package_makefile.read_text(encoding="utf-8")
        capture_lock = json.loads(args.source_lock.read_text(encoding="utf-8"))["capture"]
        expected_lock = {
            "commit": EXPECTED_COMMIT,
            "tree": EXPECTED_TREE,
            "commit_timestamp": EXPECTED_MTIME,
            "source_archive_format": "openwrt-rawgit-normalized-tar.zst",
            "source_archive_bytes": EXPECTED_ARCHIVE_BYTES,
            "source_archive_sha256": EXPECTED_ARCHIVE_SHA256,
            "source_archive_member_count": 109,
            "source_archive_members_sha256":
                "49bab41ec3c541ec353acb9dc6df244d7724bf052e72fc3a56240f63c81d51f6",
            "canonical_toolchain": {
                "git": "2.34.1",
                "git_path": "/usr/bin/git",
                "gnu_tar": "1.34",
                "gnu_tar_path": "/usr/bin/tar",
                "preserve_git_archive_modes": True,
                "zstd": "1.4.8",
                "zstd_path": "/usr/bin/zstd",
                "zstd_threads": 1,
                "zstd_ultra_level": 20,
            },
        }
        actual_lock = {name: capture_lock.get(name) for name in expected_lock}
        gates.append(gate("capture.source_lock.identity", actual_lock == expected_lock,
                          expected_lock, actual_lock))
        exact_source = all(line in makefile for line in (
            "PKG_SOURCE_PROTO:=git",
            "PKG_SOURCE_URL:=https://github.com/howtion0/MtkCSIdump-csi.git",
            f"PKG_SOURCE_VERSION:={EXPECTED_COMMIT}",
            "PKG_SOURCE_SUBMODULES:=skip",
            f"PKG_MIRROR_HASH:={EXPECTED_ARCHIVE_SHA256}",
        )) and "codeload.github.com" not in makefile and "PKG_HASH:=" not in makefile
        gates.append(gate("capture.package.git_source", exact_source,
                          "exact git commit, submodules skipped, normalized mirror hash", exact_source))
        gates.append(gate("capture.archive.bytes", len(data) == EXPECTED_ARCHIVE_BYTES,
                          EXPECTED_ARCHIVE_BYTES, len(data)))
        gates.append(gate("capture.archive.sha256", digest(data) == EXPECTED_ARCHIVE_SHA256,
                          EXPECTED_ARCHIVE_SHA256, digest(data)))
        toolchain = capture_lock.get("canonical_toolchain", {})
        version_commands = {
            "capture.toolchain.git":
                (["git", "--version"], f'git version {toolchain.get("git", "")}'),
            "capture.toolchain.gnu_tar":
                (["tar", "--version"], f'tar (GNU tar) {toolchain.get("gnu_tar", "")}'),
            "capture.toolchain.zstd":
                ([str(args.zstd), "--version"], f'v{toolchain.get("zstd", "")}'),
        }
        for name, (command, expected) in version_commands.items():
            version_proc = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=30, check=False,
            )
            first_line = version_proc.stdout.splitlines()[0] if version_proc.stdout else ""
            matches = first_line == expected if name != "capture.toolchain.zstd" \
                else expected in first_line
            gates.append(gate(name, version_proc.returncode == 0 and matches,
                              expected, first_line))
        proc = subprocess.run(
            [str(args.zstd), "-q", "-d", "-c", str(args.archive)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False,
        )
        if proc.returncode:
            raise ValueError("zstd decompression failed")
        files: list[tuple[tuple[str, ...], int, bytes]] = []
        directories: set[tuple[str, ...]] = set()
        closure: list[dict[str, Any]] = []
        seen: set[str] = set()
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                name = member.name.rstrip("/")
                pure = PurePosixPath(name)
                if (not name or name.startswith("/") or ".." in pure.parts or
                        name in seen or not pure.parts or pure.parts[0] != EXPECTED_PREFIX):
                    raise ValueError("unsafe, duplicate, or wrong-prefix tar member")
                seen.add(name)
                rel = pure.parts[1:]
                if member.isdir():
                    directories.add(rel)
                    member_type = "directory"
                    member_hash = None
                elif member.isreg() and rel:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError("regular tar member is unreadable")
                    content = extracted.read()
                    if len(content) != member.size:
                        raise ValueError("regular tar member size mismatch")
                    mode = member.mode & 0o7777
                    # Git archive emits canonical 0664/0775 members. Stage4's
                    # isolated materializer uses GNU tar --same-permissions so
                    # OpenWrt's non-root umask cannot silently reduce them.
                    # Map those modes back to Git's 100644/100755 identities.
                    if mode not in {0o664, 0o775}:
                        raise ValueError("regular tar member has unsupported Git mode")
                    git_mode = 0o100755 if mode == 0o775 else 0o100644
                    files.append((rel, git_mode, content))
                    member_type = "file"
                    member_hash = digest(content)
                else:
                    raise ValueError("archive contains symlink, hardlink, device, or special member")
                actual_mode = member.mode & 0o7777
                canonical_dir_mode = 0o755 if member.isdir() and not rel else 0o775
                if (member.uid != 0 or member.gid != 0 or member.mtime != EXPECTED_MTIME or
                        member.mode & 0o7000 or
                        (member.isdir() and actual_mode != canonical_dir_mode)):
                    raise ValueError("archive member ownership, time, or special mode is not canonical")
                closure.append({
                    "name": name,
                    "type": member_type,
                    "mode": member.mode & 0o7777,
                    "bytes": member.size,
                    "sha256": member_hash,
                })
        expected_dirs: set[tuple[str, ...]] = {()}
        for parts, _mode, _content in files:
            expected_dirs.update(parts[:index] for index in range(1, len(parts)))
        regular_only = bool(files) and directories == expected_dirs
        gates.append(gate("capture.archive.regular_members", regular_only,
                          "only regular Git files and their exact parent directories",
                          {"files": len(files), "directories": len(directories)}))
        actual_tree = tree_id(files)
        gates.append(gate("capture.git.tree", actual_tree == EXPECTED_TREE,
                          EXPECTED_TREE, actual_tree))
        closure_hash = digest(json.dumps(closure, separators=(",", ":"),
                                         sort_keys=True).encode())
        closure_actual = {"members": len(closure), "sha256": closure_hash}
        closure_expected = {
            "members": capture_lock.get("source_archive_member_count"),
            "sha256": capture_lock.get("source_archive_members_sha256"),
        }
        gates.append(gate("capture.archive.member_closure",
                          regular_only and closure_actual == closure_expected,
                          closure_expected, closure_actual))
        gates.append(gate("capture.git.commit", exact_source, EXPECTED_COMMIT,
                          EXPECTED_COMMIT if exact_source else None))
    except (OSError, UnicodeDecodeError, ValueError, tarfile.TarError,
            subprocess.TimeoutExpired) as exc:
        gates.append(gate("capture.archive.read", False, "valid canonical archive", str(exc)))
    result = "pass" if gates and all(item["status"] == "pass" for item in gates) else "fail"
    report = {
        "schema": 1,
        "classification": "public-build-input",
        "result": result,
        "archive": args.archive.name,
        "bytes": args.archive.stat().st_size if args.archive.is_file() else None,
        "sha256": digest(args.archive.read_bytes()) if args.archive.is_file() else None,
        "expected_bytes": EXPECTED_ARCHIVE_BYTES,
        "expected_sha256": EXPECTED_ARCHIVE_SHA256,
        "commit": EXPECTED_COMMIT,
        "tree": EXPECTED_TREE,
        "gates": gates,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if result == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
