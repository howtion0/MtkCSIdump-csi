# AX3000T 112 MiB single-UBI CSI firmware — Stage 4 draft

> **EXPERIMENTAL — DO NOT FLASH.** This repository is a reproducible build and
> audit draft, not a flashing recommendation. A generated image remains
> unauthorized for flashing until every machine gate passes and a human has
> independently completed the UART/recovery checklist.

This stage builds a generic Xiaomi AX3000T sysupgrade image from pinned public
sources. It combines the historical Kwrt single-UBI layout and Kwrt kernel
configuration with a hardened MediaTek CSI driver and a headless capture tool.
It deliberately contains no device backup, factory partition, MAC address,
calibration data, Wi-Fi password, or generated host key.

## Current status

- Build tooling, source locks, package recipe, verifier, publication allowlist,
  read-only layout preflight, and negative controls are implemented.
- The public Kwrt reference image is now an intentional metadata negative
  control: its stock-compatible `compat_version=1.0` and missing warning are
  rejected. It is reference-only and is not this build.
- A vanilla official OpenWrt SDK module was tested and correctly rejected:
  `.gnu.linkonce.this_module` is `0x280` and its package is APK. The required
  Kwrt ABI is `0x440`, IPK, with `kernel (=6.12.94~1-r1)`.
- This Git tree deliberately contains no firmware binary. Any separately built
  or archived image remains unsafe to flash unless its exact four-file bundle
  passes every gate documented here and receives separate human authorization.
- The final Stage 3 source is locked at `b8d7b73…`, and the dedicated Stage4
  public key/base ucert are locked. The exact-build wrapper refuses a dirty or
  uncommitted Stage4 source tree. The private key is never stored in this tree
  or any retained work volume.

## Pinned public inputs

| Input | Lock |
| --- | --- |
| OpenWrt | `v25.12.5`, commit `f0a60eee2fe051741c643ea6118718aae1ef17fb` |
| historical Kwrt wrapper | commit `aae059682faae01d600db7061c150f65de87a21e` |
| mt76 | commit `39c960c3ada558b4c2e7915772483d3731573d09` |
| historical Kwrt `.vermagic=1` transform | SHA-256 `43c09063907e10a9dd37a978be08ac5ea55774299162dd53a143963ecc1d57c5` |
| AX3000T DTS patch | SHA-256 `15bd24057e74b5335fb419fb6fc481393c34a770469c160829371ed4d20a158f` |
| upgrade patch | SHA-256 `4a98156b041f653194652e79b770dba18c4d9a840b64cb40b657c4420a412a95` |
| anti-misflash metadata patch | SHA-256 `6e8a02e357d40750e8e7440df1a04f014c0c2b9b4d030cb83cca52d2104ef3b4` |
| hardened CSI patch | SHA-256 `02d129819a662449ebb443ce5eb6b7bd38db0c99d90cd17aae75e699a9719c3e` |
| capture/localization source | commit `b8d7b73fc582795e734086a676a0a18a15980cb8` |

The complete machine-readable lock is in `source-lock.json`.

## What the image is intended to contain

- one `ubi` partition at offset `0x00600000`, size `0x07000000` (112 MiB);
- no `ubi_kernel` partition and no stock `partition@2800000` node;
- generic `nand_do_upgrade "$1"`, without an AX3000T layout-conversion hook;
- fwtool `compat_version=2.0` and the exact bilingual warning that this image
  is only for an already-verified 112 MiB single-UBI runtime and must never be
  forced onto stock dual-UBI;
- exactly one fwtool INFO plus one cryptographically verified SIGNATURE, bound
  to an independently pinned Stage4 public key copied into the final rootfs;
- Linux `6.12.94` and pinned mt76 revision `39c960c3`;
- a driver rebuilt against the exact Kwrt ABI, never the official SDK ABI;
- `/usr/sbin/mtkcsi-dump`, UDP protocol v2 and ABI documentation;
- capture-side nl80211 center-frequency/channel-width checks before and after
  each batch, a cross-poll radio-epoch drain, and audited type-5 tone ordering;
- CSI service disabled by default; no firewall opening;
- no pre-generated wireless configuration or Wi-Fi credential, and no
  first-boot script that changes a preserved user radio configuration;
- IPK signature checking enabled, with only a guaranteed-nonexistent local
  package feed (`file:///nonexistent/...`) because no compatible public package
repository exists for this custom kernel ABI.

The same locked `b8d7b73…` Git tree is carried in the canonical OpenWrt rawgit
`.tar.zst` (not GitHub's mutable codeload gzip bytes). A Stage4 materializer
fixes Git/Tar/Zstd paths, preserves Git archive modes and uses one compression
thread before OpenWrt downloads anything else, then verifies the full archive
both before and after the downloader. The archive contains the Stage3
coarse-localization and calibration toolkit (86 pytest checks, 2/2 CTest,
exact sdist-content and isolated-install/demo byte-comparison gates). Those Python algorithms remain
host-side analysis tools: the router image installs only the small headless
capture executable and its ABI/protocol documentation.

Static image inspection cannot prove the post-boot radio state after board
detection or a sysupgrade that preserves user configuration. This is recorded
as a deployment limitation, not disguised as a localization result. Publishing
the generic image does not authorize deployment; no code in this stage changes
the router, network, UCI, radio, or flash.

## Hard release gates

`verify_image.py` must prove all of the following from the final image and build
outputs:

1. exact sysupgrade identity, canonical tar EOF/padding with no hidden bytes,
   exact compat-2.0 metadata, and a pinned-key fwtool signature;
2. hashes on the FIT-selected kernel and FIT-selected DTB, a complete non-empty
   LZMA stream with no trailing bytes, and the exact nine-node SPI-NAND table
   including path, order, cells, labels, geometry and read-only flags;
3. payload fit with at least 8 MiB reserve in the 112 MiB UBI partition;
4. exact generic NAND upgrade source, no `ubi_kernel` override, and the
   `compat_version=2.0` anti-misflash barrier;
5. FIT-selected/decompressed ARM64 Linux 6.12.94 with the fixed
   `builder@buildhost` identity, final kernel config, `Module.symvers`,
   vermagic, dependency set and package manifest;
6. a vanilla control built first that is byte-identical to the 218,088-byte
   public/live baseline, with `0x440`, 294 undefined symbols, and locked hashes;
7. both vanilla and CSI modules packaged as IPK with the exact kernel
   dependency; APK is rejected;
8. the module inside the final rootfs is byte-identical to the audited module;
9. the capture binary, UDP v2 marker, ABI docs, and default-disabled service;
10. the patched module adds exactly `__nla_parse`, `nla_put`, and `skb_trim`,
    for 297 undefined symbols with the locked symbol-list hash;
11. extracted-rootfs scans for credentials, MAC addresses, private keys,
    device dumps and embedded archives, plus signed/offline feed and neutral
    release-branding gates;
12. the networked source-download phase is hash-closed before a separate
    `--network=none` build phase; provenance embeds the builder package list;
13. two independent fresh-volume builds are byte-identical for the image,
    modules, packages, configs, reports and public signing inputs;
14. provenance cross-binds the image, every report, signature identity, builder,
    tooling files, reproducibility report and clean Stage4 source commit.

See [BUILD.md](BUILD.md) for reproduction, [RELEASE.md](RELEASE.md) for the
four-asset GitHub Release policy, [PUBLIC-VS-PRIVATE.md](PUBLIC-VS-PRIVATE.md)
for the data boundary, and [RECOVERY.md](RECOVERY.md) for the mandatory human
recovery gate.

## Repository map

- `scripts/build_image.sh` — fresh-tree, fail-closed build pipeline;
- `scripts/verify_sources.py` — commits, trees, configs and patch hashes;
- `scripts/verify_vanilla_abi.py` — pre-CSI ABI control gate;
- `scripts/preflight_single_ubi.sh` — local, read-only runtime layout audit;
- `verify_image.py` — final offline image/rootfs verifier;
- `scripts/prepare_release_bundle.py` — copies exactly four public assets;
- `scripts/run_repro_pair.sh` / `compare_repro_builds.py` — mandatory two-clean-
  build byte-identity gate and canonical-output finalizer;
- `package/mtkcsi-dump/` — pinned headless capture package, disabled by default;
- `evidence/` — reference and negative-control reports, never a device dump.

## Non-goals

This work does not claim centimeter positioning, production reliability, or a
safe in-place migration from every third-party layout. It does not embed a GUI,
enable sensing automatically, alter a router, or publish any original NAND
content.

This archive currently defines **no executable sysupgrade path**. Compat 2.0
intentionally blocks stock dual-UBI and also blocks an older compat-1.0
single-UBI runtime unless someone uses force; force is forbidden here. A future
layout-aware compatibility handoff/migration requires separate authorization,
UART recovery readiness and human review. It is not part of Stage 4.
