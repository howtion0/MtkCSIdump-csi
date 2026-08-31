#!/usr/bin/env python3
"""Negative controls for the strict OpenWrt download closure."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.download_closure as download_closure
from scripts.download_closure import (
    build_closure,
    canonical_json_bytes,
    create_manifest,
    load_download_lock,
    load_manifest,
    verify_locked_closure,
)


class DownloadClosureTest(unittest.TestCase):
    @staticmethod
    def write_lock(path: Path, manifest: Path, closure: dict) -> dict:
        identity = {
            "schema": 1,
            "directories": len(closure["directories"]),
            "files": len(closure["files"]),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
        path.write_bytes(canonical_json_bytes({"builder": {"download_closure": identity}}))
        return identity

    def test_exact_names_sizes_hashes_and_empty_directories_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dl"
            (root / "nested/empty").mkdir(parents=True)
            (root / "nested/source.tar.zst").write_bytes(b"locked")
            first = build_closure(root)
            self.assertEqual(first["directories"], [".", "nested", "nested/empty"])
            self.assertEqual(first["files"][0]["bytes"], 6)
            (root / "nested/source.tar.zst").write_bytes(b"changed")
            self.assertNotEqual(first, build_closure(root))

    def test_symlink_and_special_file_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "dl"
            root.mkdir()
            (root / "regular").write_bytes(b"ok")
            (root / "link").symlink_to("regular")
            with self.assertRaisesRegex(ValueError, "symlink"):
                build_closure(root)
            (root / "link").unlink()
            os.mkfifo(root / "fifo")
            with self.assertRaisesRegex(ValueError, "special"):
                build_closure(root)

    def test_directory_swap_to_symlink_during_walk_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "dl"
            nested = root / "nested"
            outside = base / "outside"
            nested.mkdir(parents=True)
            outside.mkdir()
            (outside / "secret").write_bytes(b"outside")
            original_open = download_closure.os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "nested" and dir_fd is not None and not swapped:
                    swapped = True
                    nested.rename(root / "nested.original")
                    nested.symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(download_closure.os, "open", side_effect=racing_open):
                with self.assertRaisesRegex(ValueError, "safely open download directory"):
                    build_closure(root)
            self.assertTrue(swapped)

    def test_manifest_requires_exact_schema_and_regular_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            manifest = base / "closure.json"
            manifest.write_text(json.dumps({"schema": 1, "directories": ["."],
                                             "files": [], "extra": True}))
            with self.assertRaisesRegex(ValueError, "schema/keys"):
                load_manifest(manifest)
            manifest.unlink()
            target = base / "target.json"
            target.write_text("{}")
            manifest.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                load_manifest(manifest)

    def test_manifest_schema_rejects_boolean_true(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "closure.json"
            manifest.write_bytes(canonical_json_bytes({
                "schema": True,
                "directories": ["."],
                "files": [],
            }))
            with self.assertRaisesRegex(ValueError, "schema is not 1"):
                load_manifest(manifest)

    def test_canonical_manifest_and_locked_live_tree_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "dl"
            (root / "nested/empty").mkdir(parents=True)
            (root / "source.tar.zst").write_bytes(b"source")
            closure = build_closure(root)
            manifest = base / "download-closure.json"
            create_manifest(manifest, closure)
            self.assertEqual(manifest.read_bytes(), canonical_json_bytes(closure))
            lock = base / "source-lock.json"
            identity = self.write_lock(lock, manifest, closure)
            self.assertEqual(verify_locked_closure(root, manifest, lock), identity)

    def test_raw_manifest_bytes_are_locked_even_when_json_semantics_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "dl"
            root.mkdir()
            (root / "source").write_bytes(b"source")
            closure = build_closure(root)
            manifest = base / "download-closure.json"
            create_manifest(manifest, closure)
            lock = base / "source-lock.json"
            self.write_lock(lock, manifest, closure)
            manifest.write_bytes(manifest.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                verify_locked_closure(root, manifest, lock)

    def test_live_tree_change_is_rejected_after_identity_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "dl"
            root.mkdir()
            source = root / "source"
            source.write_bytes(b"source")
            closure = build_closure(root)
            manifest = base / "download-closure.json"
            create_manifest(manifest, closure)
            lock = base / "source-lock.json"
            self.write_lock(lock, manifest, closure)
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "download directory"):
                verify_locked_closure(root, manifest, lock)

    def test_create_is_exclusive_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "download-closure.json"
            manifest.write_bytes(b"keep")
            with self.assertRaisesRegex(ValueError, "new manifest"):
                create_manifest(manifest, {"schema": 1, "directories": ["."], "files": []})
            self.assertEqual(manifest.read_bytes(), b"keep")

    def test_duplicate_json_keys_and_non_exact_lock_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            manifest = base / "download-closure.json"
            manifest.write_text('{"schema":1,"schema":1,"directories":["."],"files":[]}')
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_manifest(manifest)
            lock = base / "source-lock.json"
            lock.write_text(
                '{"builder":{"download_closure":{"schema":1,"directories":true,'
                '"files":1,"manifest_sha256":"' + "a" * 64 + '"}}}'
            )
            with self.assertRaisesRegex(ValueError, "schema/counts"):
                load_download_lock(lock)

    def test_lock_hash_must_be_lowercase_and_lock_must_not_be_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "target.json"
            target.write_bytes(canonical_json_bytes({
                "builder": {"download_closure": {
                    "schema": 1, "directories": 1, "files": 1,
                    "manifest_sha256": "A" * 64,
                }},
            }))
            with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                load_download_lock(target)
            link = base / "source-lock.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                load_download_lock(link)


if __name__ == "__main__":
    unittest.main()
