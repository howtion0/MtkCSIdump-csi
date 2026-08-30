# Kwrt 07.15.2026 public-image evidence

The clean public image matching the installed release date is available from the Kwrt download server:

`kwrt-07.15.2026-mediatek-filogic-xiaomi_mi-router-ax3000t-squashfs-sysupgrade.bin`

This is a public source-built reference. It contains no backup data copied from the bench router.

## Immutable identity

| Item | Value |
|---|---|
| Download URL | `https://dl.openwrt.ai/releases/25.12/targets/mediatek/filogic/kwrt-07.15.2026-mediatek-filogic-xiaomi_mi-router-ax3000t-squashfs-sysupgrade.bin` |
| Size | `40,284,445` bytes |
| SHA-256 | `fdb0f654bdc5a804c296a23b6446dfb08d20bc597ac220d30800a24ce0b37e07` |
| Metadata revision | `07.15.2026` |
| Supported device | `xiaomi,mi-router-ax3000t` |
| Target | `mediatek/filogic` |

The tar has exactly `CONTROL`, `kernel`, and `root` payloads. The payload identities are:

| Payload | Size | SHA-256 |
|---|---:|---|
| `kernel` | `4,756,944` bytes | `44315619bb3c173500df0a6fe5cff77c21e4949d94f8cd048c1c3cfa2b569706` |
| `root` | `35,517,440` bytes | `68355cdf240a75b4384904f95e099fe91692574245c1728f0f8fb827397d052c` |

## Layout and upgrade-path proof

The DTB extracted from the kernel FIT identifies `xiaomi,mi-router-ax3000t` and contains this NAND partition:

```text
partition@600000 {
    label = "ubi";
    reg = <0x00600000 0x07000000>;
};
```

It contains no `ubi_kernel` partition. The packaged `platform_do_upgrade()` has no special branch for the stock board name and no `CI_KERN_UBIPART` assignment, so it reaches the generic `nand_do_upgrade` branch used by the single-UBI layout.

The image's `/lib/modules/6.12.94/mt7915e.ko` is `218,088` bytes with SHA-256 `346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621`. It is byte-identical to both the saved live module and the local rollback-package copy.

These facts make the file an exact public baseline and a suitable GitHub **Release asset** reference. They do not authorize flashing a later CSI image; every modified build must pass the same checks again.

Run the repeatable verifier with:

```bash
driver/mt7915-csi/reproduction/scripts/verify-reference-image.sh \
  /path/to/kwrt-07.15.2026-mediatek-filogic-xiaomi_mi-router-ax3000t-squashfs-sysupgrade.bin \
  /path/to/saved-live-mt7915e.ko
```

The second argument is optional. When supplied, it must remain a local comparison input; do not publish an installed-module dump or any other private recovery material.
