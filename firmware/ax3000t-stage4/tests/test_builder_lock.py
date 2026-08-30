#!/usr/bin/env python3
"""Hardware-free checks for the immutable Ubuntu builder input."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuilderSnapshotLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads((ROOT / "source-lock.json").read_text())["builder"]
        self.sources = (ROOT / "container/apt-sources.list").read_text().splitlines()
        self.dockerfile = (ROOT / "container/Dockerfile").read_text()
        self.runner = (ROOT / "scripts/run_container_build.sh").read_text()

    def test_every_apt_source_is_the_exact_signed_snapshot(self) -> None:
        snapshot = self.lock["apt_snapshot"]
        uri = f"https://snapshot.ubuntu.com/ubuntu/{snapshot}"
        keyring = self.lock["apt_archive_keyring_file"]
        options = f"check-valid-until=no signed-by={keyring}"
        expected = [
            f"deb [{options}] {uri} {suite} main restricted universe multiverse"
            for suite in ("jammy", "jammy-updates", "jammy-backports", "jammy-security")
        ]
        self.assertEqual(self.lock["apt_snapshot_uri"], uri)
        self.assertEqual(self.sources, expected)
        self.assertFalse(any("archive.ubuntu.com" in line for line in self.sources))
        self.assertFalse(any("security.ubuntu.com" in line for line in self.sources))
        self.assertFalse(any("[snapshot=" in line for line in self.sources))

    def test_archive_keyring_and_builder_files_are_hash_locked(self) -> None:
        key_hash = self.lock["apt_archive_keyring_sha256"]
        key_path = self.lock["apt_archive_keyring_file"]
        self.assertRegex(key_hash, r"^[0-9a-f]{64}$")
        self.assertIn(f"{key_hash}  {key_path}", self.dockerfile)
        for relative, field in (
            ("container/apt-sources.list", "apt_sources_sha256"),
            ("container/Dockerfile", "dockerfile_sha256"),
        ):
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, self.lock[field])
        from_lines = re.findall(r"(?m)^FROM\s+(\S+)\s*$", self.dockerfile)
        self.assertEqual(from_lines, [self.lock["base_digest"]])

    def test_runtime_uses_inspected_image_id_and_fresh_owned_volume(self) -> None:
        runner_lines = self.runner.splitlines()
        inspect_line = next(
            index for index, line in enumerate(runner_lines)
            if line.startswith('BUILDER_IMAGE_ID="$(tr -d')
        )
        after_inspect = "\n".join(runner_lines[inspect_line + 1:])
        self.assertNotIn('"$IMAGE_TAG"', after_inspect)
        self.assertEqual(after_inspect.count('\n  "$BUILDER_IMAGE_ID" \\\n  -lc'), 4)
        self.assertIn('--iidfile "$BUILDER_IID_FILE"', self.runner)
        self.assertIn("Docker did not return an immutable builder image ID", after_inspect)
        self.assertIn(
            "created Docker volume failed the exact Stage4 ownership-label check",
            after_inspect,
        )
        self.assertIn(
            "created Docker volume is not empty; refusing to reuse it",
            after_inspect,
        )
        self.assertIn("find /work -mindepth 1 -maxdepth 1 -print -quit", after_inspect)


if __name__ == "__main__":
    unittest.main()
