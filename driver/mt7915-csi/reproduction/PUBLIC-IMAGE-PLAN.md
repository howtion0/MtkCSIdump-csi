# Public image plan: safe to publish is not the same as safe to flash

The AX3000T on the bench is an RD03 running a Kwrt-specific **single-UBI 112 MiB layout**. It is not using the stock OpenWrt dual-UBI layout. This distinction is a hard release gate, not a cosmetic build option.

## Hardware layout that every candidate must match

The live device has one NAND partition named `ubi` at offset `0x00600000`, length `0x07000000` (112 MiB). Inside it are the logical volumes `kernel`, `rootfs`, and `rootfs_data`. The boot environment identifies the layout as `immortalwrt-112m`.

The historical Kwrt inputs that create this layout are:

- Kwrt commit: `aae059682faae01d600db7061c150f65de87a21e` (2026-07-15)
- OpenWrt tag/commit: `v25.12.5` / `f0a60eee2fe051741c643ea6118718aae1ef17fb`
- `devices/mediatek_filogic/patches/23-ax3000t.patch`, SHA-256 `15bd24057e74b5335fb419fb6fc481393c34a770469c160829371ed4d20a158f`
- `devices/mediatek_filogic/patches/25-platform.patch`, SHA-256 `4a98156b041f653194652e79b770dba18c4d9a840b64cb40b657c4420a412a95`

`23-ax3000t.patch` merges the stock `ubi_kernel` and `ubi` regions into one `ubi` region. `25-platform.patch` removes the AX3000T stock-layout override (`CI_KERN_UBIPART=ubi_kernel`, `CI_ROOT_UBIPART=ubi`) so the generic one-UBI NAND upgrade path is used.

## What must never be uploaded to a public GitHub repository

Do not publish raw MTD dumps, UBI dumps copied from the device, `sysupgrade -b` archives, `Factory`, `Nvram`, `Bdata`, boot-environment dumps, calibration blobs, MAC addresses, Wi-Fi credentials, SSH host keys, or router configuration. Those are private recovery material.

A public GitHub artifact must be a clean image built from source. It should contain no data read from the router.

## Offline release gates

A candidate remains `EXPERIMENTAL—DO NOT FLASH` until all of these checks pass:

1. The source manifest pins the four revisions/hashes above and the mt76 revision `39c960c3ada558b4c2e7915772483d3731573d09`.
2. The compiled DTB contains `partition@600000`, label `ubi`, and `reg = <0x00600000 0x07000000>`; it must not describe a separate `ubi_kernel` partition.
3. The compiled upgrade script sends `xiaomi,mi-router-ax3000t` through the generic `nand_do_upgrade` path and contains no AX3000T assignment to `CI_KERN_UBIPART=ubi_kernel`.
4. The sysupgrade metadata names `xiaomi,mi-router-ax3000t` in `supported_devices` and identifies the MediaTek Filogic target.
5. The sysupgrade tar contains separate `kernel` and `root` payloads whose hashes and byte counts are recorded in a release manifest.
6. Kernel release, module vermagic, mt76 source revision, toolchain (`GCC 14.3.0`), and package ABI are recorded and compared with the live baseline.
7. A rollback bundle and UART recovery procedure exist before any on-device test. Passing these offline gates does not itself authorize flashing.

## Size expectations and GitHub storage

The official stock-layout OpenWrt v25.12.5 sysupgrade is 9,400,608 bytes. It is a useful reference artifact but **must not be treated as deployable on this current layout**.

The live Kwrt `kernel` and `rootfs` UBI volumes total 44,949,504 bytes. The exact clean Kwrt 07.15.2026 public sysupgrade is 40,284,445 bytes: its kernel payload is 4,756,944 bytes and its root payload is 35,517,440 bytes. A clean feature-equivalent single-UBI CSI sysupgrade should therefore remain in roughly the 40–45 MiB band, with the exact value recorded after the source build. A large deviation is an investigation trigger, not an automatic failure by itself. See [REFERENCE-IMAGE-EVIDENCE.md](REFERENCE-IMAGE-EVIDENCE.md) for the pinned hashes and offline proof.

Publish firmware as a GitHub Release asset with a SHA-256 manifest and build provenance. Do not commit binary images directly to normal Git history, even when they are under GitHub's 100 MiB blob ceiling.
