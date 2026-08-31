#!/usr/bin/env python3
"""Create or verify a strict JSON closure for an OpenWrt dl directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


LOCK_KEYS = {"schema", "directories", "files", "manifest_sha256"}


def canonical_json_bytes(value: Any) -> bytes:
    """Return the only byte representation accepted for generated manifests."""
    return (json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_regular_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} is missing, unreadable, or symlinked") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_strict_json(path: Path, label: str) -> tuple[Any, bytes]:
    payload = read_regular_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    return value, payload


def file_identity_at(directory_fd: int, name: str, expected: os.stat_result,
                     relative: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(f"cannot safely open download file: {relative}") from exc
    value = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_dev != expected.st_dev or
                before.st_ino != expected.st_ino):
            raise ValueError(f"download file changed before hashing: {relative}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            value.update(block)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError(f"download file changed while hashing: {relative}")
        return before.st_size, value.hexdigest()
    finally:
        os.close(descriptor)


def build_closure(root: Path) -> dict[str, Any]:
    directories = ["."]
    files: list[dict[str, Any]] = []
    directory_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                       getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))

    def walk(directory_fd: int, relative: Path) -> None:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError("download traversal reached a non-directory")
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            raise ValueError("download directory is unreadable") from exc
        for entry in entries:
            name = entry.name
            if name in {".", ".."} or "\0" in name or "\n" in name or "\r" in name:
                raise ValueError("download entry has an unsafe name")
            child_rel = relative / name
            rel_text = child_rel.as_posix()
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise ValueError(f"cannot lstat download entry: {rel_text}") from exc
            if stat.S_ISLNK(mode):
                raise ValueError(f"download closure rejects symlink: {rel_text}")
            if stat.S_ISDIR(mode):
                directories.append(rel_text)
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ValueError(
                        f"cannot safely open download directory: {rel_text}"
                    ) from exc
                try:
                    opened = os.fstat(child_fd)
                    expected = entry.stat(follow_symlinks=False)
                    if (not stat.S_ISDIR(opened.st_mode) or opened.st_dev != expected.st_dev or
                            opened.st_ino != expected.st_ino):
                        raise ValueError(
                            f"download directory changed before traversal: {rel_text}"
                        )
                    walk(child_fd, child_rel)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(mode):
                size, digest = file_identity_at(
                    directory_fd, name, entry.stat(follow_symlinks=False), rel_text,
                )
                files.append({
                    "path": rel_text,
                    "bytes": size,
                    "sha256": digest,
                })
            else:
                raise ValueError(f"download closure rejects special file: {rel_text}")
        after = os.fstat(directory_fd)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise ValueError(
                f"download directory changed while traversing: {relative.as_posix()}"
            )

    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise ValueError("download root is missing, non-directory, or symlinked") from exc
    try:
        walk(root_fd, Path("."))
    finally:
        os.close(root_fd)
    if not files:
        raise ValueError("download closure contains no regular files")
    return {
        "schema": 1,
        "directories": sorted(directories, key=os.fsencode),
        "files": sorted(files, key=lambda item: os.fsencode(str(item["path"]))),
    }


def load_manifest_with_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    value, payload = load_strict_json(path, "closure manifest")
    if not isinstance(value, dict) or set(value) != {"schema", "directories", "files"}:
        raise ValueError("closure manifest schema/keys differ from the exact format")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("closure manifest schema is not 1")
    if not isinstance(value.get("directories"), list) or not isinstance(value.get("files"), list):
        raise ValueError("closure manifest directories/files are not lists")
    if payload != canonical_json_bytes(value):
        raise ValueError("closure manifest is not in the canonical JSON byte format")
    return value, payload


def load_manifest(path: Path) -> dict[str, Any]:
    value, _ = load_manifest_with_bytes(path)
    return value


def load_download_lock(path: Path) -> dict[str, Any]:
    value, _ = load_strict_json(path, "source lock")
    if not isinstance(value, dict) or not isinstance(value.get("builder"), dict):
        raise ValueError("source lock lacks a builder object")
    locked = value["builder"].get("download_closure")
    if not isinstance(locked, dict) or set(locked) != LOCK_KEYS:
        raise ValueError("source lock download closure keys differ from the exact format")
    if (type(locked.get("schema")) is not int or locked["schema"] != 1 or
            type(locked.get("directories")) is not int or locked["directories"] < 1 or
            type(locked.get("files")) is not int or locked["files"] < 1):
        raise ValueError("source lock download closure schema/counts are invalid")
    digest = locked.get("manifest_sha256")
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(character not in "0123456789abcdef" for character in digest)):
        raise ValueError("source lock download closure hash is not lowercase SHA-256")
    return locked


def verify_locked_closure(root: Path, manifest_path: Path, lock_path: Path) -> dict[str, Any]:
    expected, manifest_bytes = load_manifest_with_bytes(manifest_path)
    locked = load_download_lock(lock_path)
    identity = {
        "schema": expected["schema"],
        "directories": len(expected["directories"]),
        "files": len(expected["files"]),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    if identity != locked:
        raise ValueError("download manifest identity differs from source lock")
    actual = build_closure(root)
    if actual != expected:
        raise ValueError("download directory names/sizes/hashes differ from closure")
    return identity


def create_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) |
             getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as exc:
        raise ValueError("create requires a new manifest path") from exc
    try:
        payload = canonical_json_bytes(value)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--lock", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "create":
            if args.lock is not None:
                raise ValueError("create does not accept --lock; verify the new manifest separately")
            actual = build_closure(args.root)
            create_manifest(args.manifest, actual)
            identity = {
                "schema": actual["schema"],
                "directories": len(actual["directories"]),
                "files": len(actual["files"]),
                "manifest_sha256": hashlib.sha256(canonical_json_bytes(actual)).hexdigest(),
            }
        else:
            if args.lock is None:
                raise ValueError("verify requires --lock")
            identity = verify_locked_closure(args.root, args.manifest, args.lock)
        print(json.dumps({
            "result": "pass",
            "directories": identity["directories"],
            "files": identity["files"],
            "manifest_sha256": identity["manifest_sha256"],
        }, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"result": "fail", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
