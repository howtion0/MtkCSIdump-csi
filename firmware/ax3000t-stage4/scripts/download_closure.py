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


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def build_closure(root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("download root is missing, non-directory, or symlinked")
    directories = ["."]
    files: list[dict[str, Any]] = []

    def walk(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
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
                walk(Path(entry.path), child_rel)
            elif stat.S_ISREG(mode):
                path = Path(entry.path)
                files.append({
                    "path": rel_text,
                    "bytes": entry.stat(follow_symlinks=False).st_size,
                    "sha256": file_hash(path),
                })
            else:
                raise ValueError(f"download closure rejects special file: {rel_text}")

    walk(root, Path("."))
    if not files:
        raise ValueError("download closure contains no regular files")
    return {
        "schema": 1,
        "directories": sorted(directories, key=os.fsencode),
        "files": sorted(files, key=lambda item: os.fsencode(str(item["path"]))),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("closure manifest is missing, non-regular, or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("closure manifest is unreadable or invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "directories", "files"}:
        raise ValueError("closure manifest schema/keys differ from the exact format")
    if value.get("schema") != 1:
        raise ValueError("closure manifest schema is not 1")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        actual = build_closure(args.root)
        if args.mode == "create":
            if args.manifest.exists() or args.manifest.is_symlink():
                raise ValueError("create requires a new manifest path")
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        else:
            expected = load_manifest(args.manifest)
            if actual != expected:
                raise ValueError("download directory names/sizes/hashes differ from closure")
        print(json.dumps({
            "result": "pass",
            "directories": len(actual["directories"]),
            "files": len(actual["files"]),
        }, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"result": "fail", "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
