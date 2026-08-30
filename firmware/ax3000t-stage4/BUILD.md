# Reproducible build

> **EXPERIMENTAL — DO NOT FLASH.** A successful compilation is not sufficient.
> Only a complete passing gate report may be packaged for public archival, and
> publication still does not authorize flashing.

## Host requirements

The supported path is the supplied linux/amd64 Ubuntu 22.04 container, whose
base is locked by digest in `source-lock.json`. Its APT sources are locked to
the direct Ubuntu snapshot URI ending in `20260830T000000Z`; all four suites
use only that HTTPS origin, disable expiry for historical replay, and name the
base image's exact hash-gated Ubuntu archive keyring with `signed-by`. Both the
exact sources file and Dockerfile are hash-gated, and
`SOURCE_DATE_EPOCH=1782770622` fixes builder/image build timestamps. Provenance
embeds those identities plus the complete installed package/version list. The
wrapper requires at least
25 GiB free, a clean committed Stage4 source tree, a fresh named volume, and a
new/empty output directory. It defaults to `JOBS=2` and rejects values above 6;
the conservative default is sized for the currently available 8.3 GiB Docker
VM, not the host's nominal memory.

```sh
cd /path/to/MtkCSIdump-csi/firmware/ax3000t-stage4
chmod 0600 /private/path/ax3000t-stage4
STAGE4_SIGNING_KEY_FILE=/private/path/ax3000t-stage4 \
  JOBS=2 scripts/run_repro_pair.sh
```

`run_repro_pair.sh` invokes the container wrapper twice with two fresh uniquely
named volumes. For each clean build, phase 1 alone has networking: it fetches
the locked commits, applies the locked public patches, runs source gates and
downloads every hash-checked source. It writes a download closure receipt.
Phase 2 receives the same volume with `--network=none`, revalidates that closure,
then compiles, signs and verifies. The private key is mounted read-only only in
phase 2 and copied only to a 1 MiB tmpfs; it never enters the retained work
volume, output, log or provenance.

The volumes are required because the normal macOS host filesystem is
case-insensitive and OpenWrt correctly refuses to build there. The wrappers
refuse reused volumes/non-empty outputs and never prune Docker state. To keep
peak disk use bounded, the pair runs sequentially: after a build's exported
output passes its image/provenance/hash checks, it removes only the exact volume
it created after rechecking its unique name and ownership/session labels. A
failed build keeps its volume for diagnosis. No unrelated Docker volume is ever
selected or removed. Direct/native `build_image.sh` invocation is not a
supported release path because it would not prove the Docker network boundary
or two-clean-build requirement.

The exact build is fail-closed until `source-lock.json` says signing status
`READY`, `keys/ax3000t-stage4.pub` and `keys/ax3000t-stage4.ucert` match their
locked hashes/fingerprint, and `STAGE4_SIGNING_KEY_FILE` is a non-symlink regular
0600 file whose `usign -F` fingerprint matches. `ucert` contains wall-clock
validity data, so the base certificate is generated once and locked as bytes;
it is never regenerated in a clean build.

## Configuration derivation

The build does not use a minimal official OpenWrt SDK seed. It fetches Kwrt at
the locked historical commit and concatenates, in order:

1. `devices/common/.config`;
2. `devices/mediatek_filogic/.config`.

Their hashes are locked in `source-lock.json`. The script then removes only
multi/all-profile selectors and target-device selectors, and appends
`build-config.seed` to choose exactly one non-ubootmod AX3000T profile and the
`mtkcsi-dump` package. It also replaces any inherited kernel build identity
with `CONFIG_KERNEL_BUILD_USER="builder"` and
`CONFIG_KERNEL_BUILD_DOMAIN="buildhost"`; the final FIT banner must contain
`builder@buildhost`, so the host UID and random container hostname cannot leak
into or perturb the kernel. The historical seed spells the package choice as
`CONFIG_USE_APK=n`; Kconfig serializes that disabled state after `defconfig` as
`# CONFIG_USE_APK is not set`. Any `CONFIG_USE_APK=y` result is rejected.

The source checkout is intentionally shallow. To avoid `getver.sh` degrading
the release identity to `r0-<commit>`, every make invocation receives the
locked official revision `REVISION=r33051-f5dae5ece4` and
`SOURCE_DATE_EPOCH=1782770622` as command-line variables. Final fwtool metadata
must carry that exact revision.

The live/public Kwrt package dependency uses `.vermagic=1`. That value is not a
normal OpenWrt config result: line 46 of the locked historical Kwrt `diy.sh`
replaces the upstream config hash command with `echo '1'`. The build reproduces
only that one line as `patches/10-kwrt-vermagic-one.patch`, with source-line,
script hash, patch hash, pristine-state and patched-state gates. It never runs
the rest of `diy.sh`; in particular it performs no remote feed/wget step, no
default-password change and no Wi-Fi enabling change.

The historical `25-platform.patch` byte stream itself ends without a final LF.
Its untouched source hash remains `4a9815…`; applying those raw bytes directly
causes the last hunk to fail. The source gate therefore also locks the sole
application normalization: `historical_bytes + one LF`, SHA-256 `9be8ed…`.
It proves `normalized[:-1]` is byte-identical to the historical file and the
only added byte is `0a`. The original patch file is never rewritten.

The output records both the exact concatenated input (`kwrt-exact.config`) and
the resolved configuration (`build.config`). This keeps the Kwrt kernel knobs
that determine the module structure. It does not run Kwrt's mutable remote DIY
steps or import unpinned feeds; unknown optional packages are not a substitute
for the ABI gates below.

Unsafe historical Kwrt values are explicitly removed before the overlay:
unsigned packages, disabled signature checking, the third-party
`dl.openwrt.ai` repository, and `Kiddin`/`openwrt.ai` branding. The resolved
configuration enables IPK signature checks, uses neutral `OpenWrt CSI Lab`
metadata and this repository URL, and generates only
`file:///nonexistent/ax3000t-112m-csi-packages` feeds. The final rootfs is
checked independently and may not contain any active HTTP(S) package feed.
`CONFIG_PER_FEED_REPO` is explicitly disabled, so the final IPK configuration
contains exactly one unavailable local core source instead of generating a
misleading list of unavailable per-feed repositories.

## Build phases

1. In the networked prepare container, fetch the exact OpenWrt and Kwrt commits.
2. Verify commit/tree/config/patch/package hashes.
3. Apply only the locked one-line Kwrt vermagic transform, AX3000T
   DTS/platform patches, and `compat_version=2.0` anti-misflash patch.
4. Derive the exact Kwrt configuration and assert only one target device.
5. Download sources with OpenWrt hash checking, freeze a complete JSON closure
   of every directory/file name, byte size and SHA-256, and end the networked
   container. The capture package uses OpenWrt's native `git`/`rawgit`
   normalization at the exact commit/tree. Only the canonical `.tar.zst`
   (14,026,970 bytes, SHA-256 `6f02ffbe…`) is accepted; GitHub codeload gzip
   bytes are explicitly not a stable build input.
6. Start a new `--network=none` container, revalidate the download closure and
   pinned signing identity, then build vanilla mt76 before adding CSI.
7. Require vanilla IPK, `kernel (=6.12.94~1-r1)`, `this_module=0x440`, and
   byte identity with the public/live 218,088-byte module (SHA-256
   `346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621`).
   Its 294-symbol hash remains
   `a17a1bbec220f58147a40693cc8f1b1f8079b787f6eb7a9461eb9e4b352d10fb`.
8. Only then install the hardened CSI patch, clean mt76 and build the image.
9. Require the FIT-selected kernel and DTB each to carry a verified hash, the
   decompressed kernel to be non-empty ARM64 Linux 6.12.94 with no LZMA trailing
   data and the pinned build identity, and the DTB to contain the exact nine
   SPI-NAND partitions in compiled-node order with one-cell address/size
   semantics.
10. Require the patched undefined-symbol set to be exactly 297/hash
   `9682dffc0a1dc4760fb7bb61f6c5d1b8439a7f561e191f9ab5804dd4a9aadc4d`;
   its only delta must be `__nla_parse`, `nla_put`, and `skb_trim`. Record the
   final kernel `.config` and `Module.symvers`; this ABI has no `__versions`
   section or `modversions` vermagic token.
11. Extract the final root payload and require the locked `platform.sh` to be
    present as a regular file in that payload; an external staging copy cannot
    substitute for a missing final file.
12. Freeze the final build log, full public builder package list, provenance and
    hash lists.
13. Repeat from a second fresh volume with the same locked key/cert; require
    byte identity across every release-relevant image/module/package/config/
    report input. Only `compare_repro_builds.py` may change
    `publication_ready` to true and regenerate the canonical hash closure.

The official 25.12.5 SDK control is intentionally incompatible: its module has
`this_module=0x280` and its package is APK. `scripts/verify_vanilla_abi.py`
must reject it; vermagic alone is not proof of ABI compatibility.

## Expected output

The image name is fixed:

```text
ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin
```

The audit directory also contains the vanilla and patched modules/packages,
resolved configs, package manifest, final build log, source reports, full gate
report, provenance, `AUDIT-SHA256SUMS`, and release `SHA256SUMS`.

No output is accepted if the extracted rootfs contains an enabled CSI service,
a pre-keyed or implicitly enabled Wi-Fi config, password hash, private key,
non-placeholder identity MAC, device dump/archive, active remote package feed,
disabled IPK signature check, or stale Kwrt branding. If no wireless UCI file
is present, the accepted claim is only “no preseed/no mutation”; runtime board
detection and a preserved user configuration remain deployment-time checks.

## Verify without rebuilding

Use all evidence from the same output directory:

```sh
python3 verify_image.py \
  out/ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin \
  --platform-sh out/platform.sh \
  --package-manifest out/packages.manifest \
  --module out/mt7915e.ko \
  --kmod-package out/kmod-mt7915e.ipk \
  --baseline-module out/mt7915e.vanilla.ko \
  --baseline-kmod-package out/kmod-mt7915e.vanilla.ipk \
  --kernel-release-file out/kernel.release \
  --kernel-config out/kernel.config \
  --module-symvers out/Module.symvers \
  --ucert /path/to/locked-host-tools/ucert \
  --usign /path/to/locked-host-tools/usign \
  --release-public-key keys/ax3000t-stage4.pub \
  --release-base-ucert keys/ax3000t-stage4.ucert \
  --source-lock source-lock.json \
  --unsquashfs "$(command -v unsquashfs)" \
  --output out/gate-report.json
```

Do not use `--allow-incomplete` for a candidate image. That switch exists only
to document old public reference images. Reports with warnings are labeled
`incomplete`, never `pass`, and the release bundler rejects them.

`scripts/preflight_single_ubi.sh` is a future, local, read-only human audit of
board identity, all nine MTD index/name/offset/size/erase fields, attached UBI
and `/overlay`. The compiled DT order intentionally yields `mtd7=KF` and
`mtd8=ubi`; the script checks the sysfs offsets rather than inventing offsets
by adding `/proc/mtd` rows. Do not automate it over SSH. Even a PASS does not authorize a
flash. `sysupgrade -F` is not a normal step and must never be used to bypass the
`compat_version=2.0` barrier.

## Reproducibility comparison

Two independent clean builds are a hard gate, not an optional comparison. The
final signed image is expected to be byte-identical because both authorized
builds use the same external private key and the same pinned time-bearing base
ucert. Independent builders without that private key can reproduce and compare
the signed prefix, but cannot recreate the final signature; this is the normal
boundary of a private signing identity. Any byte difference blocks publication
and neither output may silently replace the other.
