#!/usr/bin/env python3
"""Hardware-free negative controls for the final-rootfs privacy gate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from verify_image import Report, scan_root, verify_package_feeds, verify_release_branding


class PrivacyGateTest(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "etc/config").mkdir(parents=True)
        (root / "etc/config/rpcd").write_text(
            "config login\n\toption password '$p$root'\n", encoding="utf-8"
        )
        (root / "etc/config/luci").write_text(
            "config extern\n\toption passwd '/etc/passwd'\n", encoding="utf-8"
        )
        (root / "etc/shadow").write_text("root:*:0:0:99999:7:::\n", encoding="utf-8")
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/ash\n", encoding="utf-8")
        return temp, root

    def run_gate(self, root: Path) -> Report:
        report = Report(Path("fixture-sysupgrade.bin"))
        scan_root(root, report)
        return report

    def test_precise_standard_placeholders_are_safe(self) -> None:
        _, root = self.fixture()
        self.assertTrue(self.run_gate(root).ok)

    def test_real_uci_password_is_rejected_without_echoing_value(self) -> None:
        _, root = self.fixture()
        secret = "do-not-copy-this-secret"
        (root / "etc/config/network").write_text(
            f"config interface 'wan'\n\toption pppoe_password '{secret}'\n",
            encoding="utf-8",
        )
        report = self.run_gate(root)
        self.assertFalse(report.ok)
        self.assertNotIn(secret, str(report.as_json()))

    def test_private_key_marker_in_program_text_is_not_a_false_positive(self) -> None:
        _, root = self.fixture()
        program = root / "usr/lib/example.bin"
        program.parent.mkdir(parents=True)
        program.write_bytes(b"parser accepts -----BEGIN PRIVATE KEY----- markers\0")
        self.assertTrue(self.run_gate(root).ok)

    def test_complete_private_key_block_is_rejected(self) -> None:
        _, root = self.fixture()
        key = root / "etc/generated.pem"
        key.write_bytes(
            b"-----BEGIN PRIVATE KEY-----\nQUFBQUFBQUFB\nQkJCQkJCQkJC\n"
            b"-----END PRIVATE KEY-----\n"
        )
        self.assertFalse(self.run_gate(root).ok)

    def test_device_dump_filename_is_rejected(self) -> None:
        _, root = self.fixture()
        (root / "mtd0.bin").write_bytes(b"not a real dump")
        self.assertFalse(self.run_gate(root).ok)

    def test_non_placeholder_mac_is_checked_only_in_identity_config(self) -> None:
        _, root = self.fixture()
        docs = root / "usr/share/doc/example"
        docs.parent.mkdir(parents=True)
        docs.write_text("example 12:34:56:78:9a:bc", encoding="utf-8")
        self.assertTrue(self.run_gate(root).ok)
        (root / "etc/config/network").write_text(
            "config device\n\toption macaddr '00:11:22:aa:bb:cc'\n", encoding="utf-8"
        )
        self.assertFalse(self.run_gate(root).ok)

    def test_sensitive_symlink_target_is_rejected(self) -> None:
        _, root = self.fixture()
        link = root / "etc/device-copy"
        link.symlink_to("/tmp/mtd0.bin")
        self.assertFalse(self.run_gate(root).ok)

    def test_identity_config_symlink_is_rejected_even_with_benign_name(self) -> None:
        _, root = self.fixture()
        link = root / "etc/config/network"
        link.symlink_to("/tmp/generated-network")
        self.assertFalse(self.run_gate(root).ok)

    def test_symlinked_sensitive_directory_ancestors_are_rejected(self) -> None:
        for relative in ("etc/config", "lib/modules"):
            with self.subTest(relative=relative):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                root = Path(temp.name) / "root"
                outside = Path(temp.name) / "outside"
                root.mkdir()
                outside.mkdir()
                link = root / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                self.assertFalse(self.run_gate(root).ok)

    def test_non_root_shadow_hash_is_rejected(self) -> None:
        _, root = self.fixture()
        (root / "etc/shadow").write_text(
            "root:*:0:0:99999:7:::\nadmin:$6$not-public:0:0:99999:7:::\n",
            encoding="utf-8",
        )
        report = self.run_gate(root)
        self.assertFalse(report.ok)
        self.assertNotIn("$6$not-public", str(report.as_json()))

    def test_passwd_embedded_hash_is_rejected(self) -> None:
        _, root = self.fixture()
        (root / "etc/passwd").write_text(
            "root:x:0:0:root:/root:/bin/ash\nadmin:embedded-hash:1000:1000::/:/bin/false\n",
            encoding="utf-8",
        )
        self.assertFalse(self.run_gate(root).ok)

    def test_authorized_keys_is_rejected(self) -> None:
        _, root = self.fixture()
        key = root / "etc/dropbear/authorized_keys"
        key.parent.mkdir(parents=True)
        key.write_text("ssh-ed25519 AAAA fixture\n", encoding="utf-8")
        self.assertFalse(self.run_gate(root).ok)


class PackageFeedGateTest(unittest.TestCase):
    def run_gate(self, remote: bool = False, *, present_local: bool = False,
                 traversal: bool = False) -> Report:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        opkg = root / "etc/opkg"
        opkg.mkdir(parents=True)
        (root / "etc/opkg.conf").write_text("option check_signature\n", encoding="utf-8")
        repo = "https://dl.openwrt.ai/releases/25.12" if remote else (
            "file:///nonexistent/ax3000t-112m-csi-packages/../escape"
            if traversal else
            "file:///nonexistent/ax3000t-112m-csi-packages/targets/mediatek/filogic/packages"
        )
        (opkg / "distfeeds.conf").write_text(
            f"src/gz openwrt-csi-lab_core {repo}\n",
            encoding="utf-8",
        )
        (opkg / "customfeeds.conf").write_text(
            "# src/gz example https://example.invalid/comment-only\n", encoding="utf-8"
        )
        if present_local:
            local = root / "nonexistent/ax3000t-112m-csi-packages/targets/mediatek/filogic/packages"
            local.mkdir(parents=True)
            (local / "Packages").write_text("Package: unexpected\n", encoding="utf-8")
        report = Report(Path("fixture-sysupgrade.bin"))
        verify_package_feeds(root, report)
        return report

    def test_nonexistent_local_feed_and_signature_check_pass(self) -> None:
        self.assertTrue(self.run_gate().ok)

    def test_active_remote_feed_is_rejected(self) -> None:
        self.assertFalse(self.run_gate(remote=True).ok)

    def test_present_local_feed_is_rejected(self) -> None:
        self.assertFalse(self.run_gate(present_local=True).ok)

    def test_path_traversal_feed_is_rejected(self) -> None:
        self.assertFalse(self.run_gate(traversal=True).ok)


class ReleaseBrandingGateTest(unittest.TestCase):
    def run_gate(self, stale: bool = False) -> Report:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "etc").mkdir()
        (root / "usr/lib").mkdir(parents=True)
        manufacturer = "Kiddin' https://openwrt.ai/" if stale else \
            "OpenWrt CSI Lab https://github.com/howtion0/MtkCSIdump-csi"
        for rel in ("etc/openwrt_release", "etc/device_info", "usr/lib/os-release"):
            (root / rel).write_text(manufacturer + "\n", encoding="utf-8")
        report = Report(Path("fixture-sysupgrade.bin"))
        verify_release_branding(root, report)
        return report

    def test_neutral_public_branding_passes(self) -> None:
        self.assertTrue(self.run_gate().ok)

    def test_historical_kwrt_branding_is_rejected(self) -> None:
        self.assertFalse(self.run_gate(stale=True).ok)


if __name__ == "__main__":
    unittest.main()
