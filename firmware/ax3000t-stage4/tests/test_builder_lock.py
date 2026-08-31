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
        self.builder = (ROOT / "scripts/build_image.sh").read_text()

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

    def test_ca_bootstrap_is_exact_and_never_disables_tls_checks(self) -> None:
        bootstrap = self.lock["ca_bootstrap"]
        path = "/tmp/ca-certificates-bootstrap.deb"
        self.assertEqual(bootstrap["package"], "ca-certificates")
        self.assertEqual(bootstrap["version"], "20260601~22.04.1")
        self.assertEqual(bootstrap["bytes"], 140666)
        self.assertEqual(bootstrap["certificate_count"], 121)
        self.assertIn(
            f"ADD --checksum=sha256:{bootstrap['sha256']} \\\n"
            f"    {bootstrap['url']} \\\n"
            f"    {path}",
            self.dockerfile,
        )
        self.assertIn(f"{bootstrap['sha256']}  {path}", self.dockerfile)
        self.assertIn(f"{bootstrap['sha512']}  {path}", self.dockerfile)
        self.assertIn(
            f"{bootstrap['bundle_sha256']}  /etc/ssl/certs/ca-certificates.crt",
            self.dockerfile,
        )
        for forbidden in (
            "http://snapshot.ubuntu.com", "Acquire::https::Verify-Peer",
            "Acquire::https::Verify-Host", "--no-check-certificate",
        ):
            self.assertNotIn(forbidden, self.dockerfile)

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

    def test_generated_volume_names_stay_inside_lowercase_namespace(self) -> None:
        pair_runner = (ROOT / "scripts/run_repro_pair.sh").read_text()
        self.assertIn("date -u +%Y%m%dt%H%M%Sz", self.runner)
        self.assertIn("date -u +%Y%m%dt%H%M%Sz", pair_runner)
        self.assertNotIn("date -u +%Y%m%dT%H%M%SZ", self.runner)
        self.assertNotIn("date -u +%Y%m%dT%H%M%SZ", pair_runner)

    def test_capture_archive_materializer_locks_modes_paths_and_compressor(self) -> None:
        toolchain = json.loads((ROOT / "source-lock.json").read_text())["capture"][
            "canonical_toolchain"
        ]
        self.assertEqual(toolchain, {
            "git": "2.34.1",
            "git_path": "/usr/bin/git",
            "gnu_tar": "1.34",
            "gnu_tar_path": "/usr/bin/tar",
            "preserve_git_archive_modes": True,
            "zstd": "1.4.8",
            "zstd_path": "/usr/bin/zstd",
            "zstd_threads": 1,
            "zstd_ultra_level": 20,
        })
        for token in (
            '/usr/bin/tar --same-permissions -C "$CAPTURE_TREE_DIR"',
            '/usr/bin/zstd -q -T1 --ultra -20 -c',
            'CAPTURE_ARCHIVE_BYTES="14026970"',
            'CAPTURE_ARCHIVE_SHA256="6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18"',
        ):
            self.assertIn(token, self.builder)
        preseed = self.builder.index('mv "$CAPTURE_ARCHIVE_TMP" "$CAPTURE_ARCHIVE"')
        download = self.builder.index('"${MAKE[@]}" -j"$JOBS" download')
        self.assertLess(preseed, download)
        self.assertEqual(
            self.builder.count('python3 "$STAGE_DIR/scripts/verify_capture_archive.py"'),
            2,
        )

    def test_canonical_builder_vanilla_is_not_mislabeled_as_live_byte_identity(self) -> None:
        source_lock = json.loads((ROOT / "source-lock.json").read_text())
        live = source_lock["live_abi_baseline"]
        canonical = source_lock["canonical_builder_vanilla"]
        self.assertEqual((live["module_bytes"], live["module_sha256"]), (
            218088,
            "346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621",
        ))
        self.assertEqual((canonical["module_bytes"], canonical["module_sha256"]), (
            217976,
            "e9eb76d14a51257e6d50aa8c50a1b6c97351e3395595ed243bc0c7d2033e9309",
        ))
        self.assertNotEqual(
            (live["module_bytes"], live["module_sha256"]),
            (canonical["module_bytes"], canonical["module_sha256"]),
        )
        self.assertEqual(set(canonical), {"module_bytes", "module_sha256"})
        verifier = (ROOT / "verify_image.py").read_text()
        self.assertIn(
            'EXPECTED_BUILDER_VANILLA_MODULE_BYTES = 217976', verifier
        )
        self.assertIn(canonical["module_sha256"], verifier)
        self.assertIn(live["module_sha256"], verifier)
        self.assertNotIn(
            "vanilla module is byte-identical to the public/live Kwrt baseline",
            verifier,
        )
        self.assertIn('"module.vanilla.canonical_builder_fingerprint"', verifier)
        self.assertNotIn('"module.baseline.byte_identity"', verifier)
        build_doc = (ROOT / "BUILD.md").read_text()
        for diagnostic_fact in (
            "112-byte size delta", "`ASSERT_RTNL()` `__FILE__` path string",
            "`.rodata.str1.8`",
        ):
            self.assertIn(diagnostic_fact, build_doc)

    def test_prepare_download_closure_and_offline_build_order_are_exact(self) -> None:
        expected_targets = [
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
        ]
        self.assertEqual(self.lock["prepare_download_targets"], expected_targets)
        self.assertEqual(self.lock["prepare_dependency_closure"], {
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
        })
        self.assertEqual(self.lock["download_closure"], {
            "schema": 1,
            "directories": 1,
            "files": 132,
            "manifest_sha256": (
                "fd5f9a233c2313a5e4f5f7391aeb7be5f35b67537b657c30d861c4adb26c345c"
            ),
        })
        self.assertEqual(self.lock["serial_package_prerequisites"], [{
            "target": "package/utils/lua/compile",
            "jobs": 1,
            "reason": (
                "Lua 5.1 package compilation can leave zero-byte objects under "
                "inherited parallelism"
            ),
        }])
        self.assertEqual(self.lock["vanilla_mt76_compile"], {
            "target": "package/kernel/mt76/compile",
            "jobs": 1,
            "reason": (
                "The direct ABI-control goal expands selected network dependencies that race "
                "under parallel package compilation"
            ),
        })
        self.assertNotIn("package/libs/ncurses/host/download", self.builder)
        self.assertNotIn("package/system/ucert/host/download", self.builder)
        self.assertNotIn("CHECK_ALL", self.builder)
        self.assertIn(
            "grep -qx '# CONFIG_BUILD_ALL_HOST_TOOLS is not set' \"$OPENWRT/.config\"",
            self.builder,
        )
        self.assertIn(
            "grep -qx '# CONFIG_TARGET_INITRAMFS_COMPRESSION_LZ4 is not set' "
            '"$OPENWRT/.config"',
            self.builder,
        )
        array_match = re.findall(
            r'(?ms)^PREPARE_DOWNLOAD_TARGETS=\(\n(.*?)^\)\n', self.builder
        )
        self.assertEqual(len(array_match), 1)
        self.assertEqual(
            array_match[0].splitlines(),
            [f'  "{target}"' for target in expected_targets],
        )
        serial_loop = (
            'for prepare_download_target in "${PREPARE_DOWNLOAD_TARGETS[@]}"; do\n'
            "  printf '[networked-prepare] exact download target: %s\\n' \\\n"
            '    "$prepare_download_target" | tee -a "$LOG"\n'
            '  "${MAKE[@]}" -j1 "$prepare_download_target" 2>&1 | tee -a "$LOG"\n'
            "done"
        )
        self.assertEqual(self.builder.count(serial_loop), 1)
        general_download = self.builder.index(
            '"${MAKE[@]}" -j"$JOBS" download 2>&1 | tee -a "$LOG"'
        )
        exact_downloads = self.builder.index(serial_loop)
        short_file_gate = self.builder.index(
            'if find "$OPENWRT/dl" -type f -size -1024c -print -quit'
        )
        closure_create = self.builder.index(
            'python3 "$STAGE_DIR/scripts/download_closure.py" create \\\n'
        )
        self.assertLess(general_download, exact_downloads)
        self.assertLess(exact_downloads, short_file_gate)
        self.assertLess(short_file_gate, closure_create)

        closure_marker = 'python3 "$STAGE_DIR/scripts/download_closure.py" verify \\\n'
        closure_block = (
            'python3 "$STAGE_DIR/scripts/download_closure.py" verify \\\n'
            '  --root "$OPENWRT/dl" --manifest "$WORK_DIR/download-closure.json" \\\n'
            '  --lock "$STAGE_DIR/source-lock.json" | tee -a "$LOG"'
        )
        closure_positions = [
            match.start() for match in re.finditer(re.escape(closure_marker), self.builder)
        ]
        self.assertEqual(len(closure_positions), 3)
        self.assertEqual(self.builder.count(closure_block), 3)
        receipt_create_marker = 'python3 - "$WORK_DIR/.stage4-prepared.json"'
        receipt_validate_marker = 'python3 - "$PREPARED_MARKER"'
        self.assertEqual(self.builder.count(receipt_create_marker), 1)
        self.assertEqual(self.builder.count(receipt_validate_marker), 1)
        receipt_create = self.builder.index(receipt_create_marker)
        receipt_validate = self.builder.index(receipt_validate_marker)
        for token in (
            'locked_download_manifest_sha256 = json.loads(',
            ')["builder"]["download_closure"]["manifest_sha256"]',
            'if download_manifest_sha256 != locked_download_manifest_sha256:',
            'raise SystemExit("download manifest differs from the locked closure before '
            'receipt creation")',
        ):
            self.assertIn(token, self.builder)
        self.assertEqual(self.lock["offline_prepare_target"], "prepare")
        prepare = self.builder.index(
            '"${MAKE[@]}" -j"$JOBS" prepare 2>&1 | tee -a "$LOG"'
        )
        usign = self.builder.index(
            '"${MAKE[@]}" -j"$JOBS" package/system/usign/host/compile '
            '2>&1 | tee -a "$LOG"'
        )
        ucert = self.builder.index(
            '"${MAKE[@]}" -j"$JOBS" package/system/ucert/host/compile '
            '2>&1 | tee -a "$LOG"'
        )
        lua = self.builder.index(
            '"${MAKE[@]}" -j1 package/utils/lua/compile 2>&1 | tee -a "$LOG"'
        )
        for token in (
            "verify_lua_serial_artifacts() {",
            "mapfile -t LUA_SOURCE_DIRS < <(find \"$OPENWRT/build_dir\" -type d",
            "[[ ${#LUA_SOURCE_DIRS[@]} -eq 1 ]] || {",
            "-path '*/lua-5.1.5/src' | sort)",
            "-name '*.o' -size 0",
            "Lua serial prerequisite left a zero-byte object",
            '[[ -s "${LUA_SOURCE_DIRS[0]}/liblua.so.5.1.5" ]] || {',
        ):
            self.assertEqual(self.builder.count(token), 1)
        lua_pre_mt76 = self.builder.index(
            'verify_lua_serial_artifacts "before vanilla mt76"'
        )
        final_image_build = self.builder.index(
            'if ! "${MAKE[@]}" -j"$JOBS" 2>&1 | tee -a "$LOG"; then'
        )
        lua_post_image = self.builder.index(
            'verify_lua_serial_artifacts "after final image build"'
        )
        target_output = self.builder.index(
            'TARGET_OUT="$OPENWRT/bin/targets/mediatek/filogic"'
        )
        mt76 = self.builder.index(
            '"${MAKE[@]}" -j1 package/kernel/mt76/compile 2>&1 | tee -a "$LOG"'
        )
        self.assertNotIn('-j"$JOBS" package/kernel/mt76/compile', self.builder)
        vanilla_gate = self.builder.index(
            'python3 "$STAGE_DIR/scripts/verify_vanilla_abi.py" \\\n'
        )
        csi_patch = self.builder.index(
            'cp "$STAGE_DIR/patches/999-mt7915-csi-v2-hardened.patch" \\\n'
        )
        self.assertEqual(
            [closure_create, closure_positions[0], receipt_create,
             receipt_validate, closure_positions[1], prepare, closure_positions[2],
             usign, ucert, lua, lua_pre_mt76, mt76, vanilla_gate, csi_patch,
             final_image_build, lua_post_image, target_output],
            sorted([closure_create, closure_positions[0], receipt_create,
                    receipt_validate, closure_positions[1], prepare,
                    closure_positions[2], usign, ucert, lua, lua_pre_mt76, mt76,
                    vanilla_gate, csi_patch, final_image_build, lua_post_image,
                    target_output]),
        )
        self.assertNotIn("tools/install", self.builder)
        self.assertNotIn("toolchain/install", self.builder)


if __name__ == "__main__":
    unittest.main()
