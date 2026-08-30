#!/usr/bin/env python3
"""Negative controls for the strict OpenWrt download closure."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.download_closure import build_closure, load_manifest


class DownloadClosureTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
