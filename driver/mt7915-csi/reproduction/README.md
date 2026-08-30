# AX3000T CSI Driver · Stage 2 ABI Reproduction

This directory is the offline, fail-closed reproduction record for the Stage 2 MT7915 CSI driver. It answers two separate questions:

1. Can the pinned upstream driver and the hardened CSI patch compile in a known OpenWrt SDK?
2. Does that SDK output match the ABI of the module installed by the pinned Kwrt firmware?

The answer to the first question is **yes**. The answer to the second is **no**: the official SDK is a compile-control environment only. Nothing produced by these scripts is approved for installation or flashing.

The scripts do not connect to a router, unload Wi-Fi, change host routes, or write NAND. Generated outputs live under ignored `out/` and `report/` directories.

## Pinned baseline

| Component | Pinned value |
|---|---|
| OpenWrt | `v25.12.5` / `f0a60eee2fe051741c643ea6118718aae1ef17fb` |
| Kernel | `6.12.94` |
| mt76 | `39c960c3ada558b4c2e7915772483d3731573d09` |
| Toolchain | GCC `14.3.0`, musl, `aarch64_cortex-a53` |
| Official SDK size | `252397003` bytes |
| Official SDK SHA-256 | `ff4a38a397caa2cfe1c39e18f84ddede14878221b3593c3f2c4cfe24e3ec4c25` |
| Installed/public-reference `mt7915e.ko` size | `218088` bytes |
| Installed/public-reference module SHA-256 | `346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621` |

The public Kwrt 07.15.2026 image contains a module byte-identical to the separately saved installed and rollback-package copies. See [REFERENCE-IMAGE-EVIDENCE.md](REFERENCE-IMAGE-EVIDENCE.md). Private recovery material is evidence only and must never be added to this repository or a GitHub release.

## Required inputs

The build is intentionally explicit. It has no machine-specific default paths.

- `SOURCE_ROOT`: an unmodified OpenWrt checkout at commit `f0a60eee2fe051741c643ea6118718aae1ef17fb`. The script checks the commit, the three copied package directories, and the pinned mt76 source revision/hash before building.
- `SDK_ARCHIVE`: the verified OpenWrt v25.12.5 MediaTek Filogic SDK archive.
- Docker with `linux/amd64` emulation and named-volume support.

The official SDK contains Linux/x86-64 host tools and requires a case-sensitive filesystem. The build therefore runs as `linux/amd64` in a fresh Docker named volume. Source and SDK inputs are mounted read-only; generated state is exported to an ignored output directory.

From the repository root, verify the bundled small-source manifest before use:

```bash
shasum -a 256 -c driver/mt7915-csi/reproduction/SMALL-FILES.sha256
# GNU/Linux equivalent: sha256sum -c driver/mt7915-csi/reproduction/SMALL-FILES.sha256
```

## Reproduce the controls

From the repository root:

```bash
export SOURCE_ROOT=/path/to/openwrt-at-f0a60eee
export SDK_ARCHIVE=/path/to/official-openwrt-sdk-25.12.5.tar.zst

driver/mt7915-csi/reproduction/scripts/verify-sdk.sh "$SDK_ARCHIVE"

WORK_VOLUME=ax3000t-mt76-vanilla-clean-25125 \
  driver/mt7915-csi/reproduction/scripts/build-vanilla-mt7915e.sh
```

The build script refuses to reuse or delete a Docker volume and refuses to overwrite a non-empty output directory. Choose a new `WORK_VOLUME` and `OUTPUT_DIR` for each independent run. It exports raw and APK-extracted modules, APK metadata, kernel config, `Module.symvers`, package-selection config, verbose logs, and hashes under `reproduction/out/`.

To compare against a locally held installed-module copy without publishing that private evidence:

```bash
driver/mt7915-csi/reproduction/scripts/compare-module.sh \
  /path/to/saved-live-mt7915e.ko \
  driver/mt7915-csi/reproduction/out/vanilla/mt7915e.packaged.ko \
  driver/mt7915-csi/reproduction/report/vanilla-vs-live
```

The hardened source-compilation control uses the repository's Stage 2 patch by default (`../patches/0001-mt7915-csi-v2-hardened.patch` relative to this directory):

```bash
WORK_VOLUME=ax3000t-mt76-csi-hardened-sdk-clean-25125 \
OUTPUT_DIR="$PWD/driver/mt7915-csi/reproduction/out/patched-sdk-control-clean" \
  driver/mt7915-csi/reproduction/scripts/build-hardened-sdk-control.sh
```

`MT76_PATCH` may override the patch path, but `EXPECTED_MT76_PATCH_SHA256` must also match it. The repository default is pinned to SHA-256 `02d129819a662449ebb443ce5eb6b7bd38db0c99d90cd17aae75e699a9719c3e`.

## Result and hard ABI gate

Both controls compile, but the official SDK outputs fail deployment compatibility:

| Gate | Installed/public Kwrt | Official SDK |
|---|---:|---:|
| `.gnu.linkonce.this_module` | `0x440` | `0x280` |
| Package format | IPK | APK v3 |
| Kernel dependency | `kernel (=6.12.94~1-r1)` | `kernel=6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1` |
| `vermagic` | `6.12.94 SMP mod_unload aarch64` | same |

Matching `vermagic` is not enough. The `struct module` layout and package/kernel ABI differ, so neither official-SDK module may be installed or embedded in a firmware image. [RESULTS.md](RESULTS.md) records the complete vanilla and hardened comparisons.

## Acceptance criteria for a later deployable build

- Build through the pinned Kwrt source/configuration and IPK packaging chain.
- Match `.gnu.linkonce.this_module=0x440`, kernel dependency `6.12.94~1-r1`, module dependencies, and the expected external-symbol ABI.
- Verify the single-UBI 112 MiB AX3000T layout and generic NAND upgrade path independently.
- Produce hashes, provenance, and a rollback plan before any on-device test.
- Keep every image `EXPERIMENTAL—DO NOT FLASH` until all offline gates and an explicitly authorized device-validation stage pass.

Firmware images belong only in a GitHub **Release asset** with SHA-256 and provenance; binary images must not be committed to normal Git history. See [PUBLIC-IMAGE-PLAN.md](PUBLIC-IMAGE-PLAN.md).
