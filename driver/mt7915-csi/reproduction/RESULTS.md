# Stage 2 ABI reproduction results: exact reference found, official SDK control rejected for deployment

## Bottom line

The public Kwrt 07.15.2026 sysupgrade is an exact full-image reference for the installed release: its `mt7915e.ko` is byte-identical to the saved live module. The official OpenWrt v25.12.5 SDK can compile the same vanilla mt76 revision and produces the same module metadata and undefined-symbol set, but it does **not** reproduce the installed Kwrt kernel-module ABI. Its output is compile-only evidence and must not be installed or used to seal a firmware image.

No router, Wi-Fi interface, macOS route, or NAND partition was touched during this stage.

## Vanilla control comparison

| Evidence | Installed/public Kwrt | Official SDK control |
|---|---:|---:|
| Packaged `mt7915e.ko` size | `218,088` bytes | `215,200` bytes |
| Packaged `mt7915e.ko` SHA-256 | `346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621` | `3f256d99ce694f883cce22152da08de152f7fe1829afcab6acdbc80ac08eabc1` |
| `.gnu.linkonce.this_module` | `0x440` | `0x280` |
| `vermagic` | `6.12.94 SMP mod_unload aarch64` | identical |
| `depends` | `mt76-connac-lib,mt76,mac80211,cfg80211` | identical |
| Non-empty undefined symbols | `294` | `294` |
| Undefined-symbol-list SHA-256 | `a17a1bbec220f58147a40693cc8f1b1f8079b787f6eb7a9461eb9e4b352d10fb` | identical |
| Package format | Kwrt IPK | official SDK APK v3 |
| Kernel package dependency | `kernel (=6.12.94~1-r1)` | `kernel=6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1` |

The matching symbol set shows that the vanilla driver source is aligned at the external-symbol level. It does not override the `struct module` layout mismatch. Linux checks more than the visible version string, so matching `vermagic` is not evidence that this APK module is loadable on the Kwrt kernel.

The SDK package also renames local function symbols during its strip/package pass. That contributes to byte-level differences but does not explain away the `this_module` mismatch.

## Pinned SDK context

| Input | SHA-256 |
|---|---|
| Official SDK archive (`252,397,003` bytes) | `ff4a38a397caa2cfe1c39e18f84ddede14878221b3593c3f2c4cfe24e3ec4c25` |
| SDK kernel `.config` | `0a5f3999395978e021d183ecc3410869b5e97442b20e096110ca5fdc7a56dbb1` |
| SDK `Module.symvers` | `c399fe956038c3362d0418267663fdf2ff4cd5dde41f3e182b93964719600139` |
| mt76 source archive | `7a9f8ea21eee5324e6638ace627dd305b3650ae6ca86109317d9ee83702140eb` |

The SDK raw module is `1,644,472` bytes with SHA-256 `657e3b9591f6dad9dcd1f476bbb05f9998cb1113f172063066a8f10de9885094`. The APK is `78,761` bytes with SHA-256 `84dcbceaaebc8141ade72be16526879a8b8d3f3497cce65fa1b9ba0bfe45c368`.

## Evidence paths

- Vanilla binaries, package metadata, configs, and build logs: `reproduction/out/vanilla/`
- Fail-closed live comparison: `reproduction/report/vanilla-vs-live/`
- Complete verbose SDK build log: `reproduction/out/vanilla/pruned-vanilla-mt76-verbose.log`
- Public-image layout proof: [REFERENCE-IMAGE-EVIDENCE.md](REFERENCE-IMAGE-EVIDENCE.md)

## Deployment gate

A deployment candidate must be produced by the pinned Kwrt `aae059682faae01d600db7061c150f65de87a21e` build flow with its generated kernel `.config`, `Module.symvers`, IPK packaging, and the two single-UBI layout patches. Until that chain emits a module whose `this_module` layout matches `0x440`, the candidate remains `COMPILE-ONLY—DO NOT INSTALL`.

The hardened CSI SDK result will be recorded below only as a controlled source-compilation experiment; it cannot clear this deployment gate.

## Hardened CSI SDK control

The hardened patch compiled successfully in a separate fresh Docker volume, `ax3000t-mt76-csi-hardened-sdk-25125-20260831`. This is a **compile-only source control**, not a deployment artifact.

| Evidence | Vanilla official-SDK control | Hardened CSI official-SDK control |
|---|---:|---:|
| Raw `mt7915e.ko` size | `1,644,472` | `1,768,192` |
| Raw `mt7915e.ko` SHA-256 | `657e3b9591f6dad9dcd1f476bbb05f9998cb1113f172063066a8f10de9885094` | `1c93e4715410a62ee47a6ad391af08d5f105181a2ddaee26b857afd25f449956` |
| Packaged `mt7915e.ko` size | `215,200` | `229,000` |
| Packaged `mt7915e.ko` SHA-256 | `3f256d99ce694f883cce22152da08de152f7fe1829afcab6acdbc80ac08eabc1` | `0d620dda7b8534dbe0f4acdb79bad7253cdf6947efa57eaae3f757874d4638ee` |
| APK size | `78,761` | `84,773` |
| APK SHA-256 | `84dcbceaaebc8141ade72be16526879a8b8d3f3497cce65fa1b9ba0bfe45c368` | `a864cff35a1a285c989063fb70bc47c6b382971cdb0e8b09e65157b43892a80e` |
| Non-empty undefined symbols | `294` | `297` |
| Undefined-symbol-list SHA-256 | `a17a1bbec220f58147a40693cc8f1b1f8079b787f6eb7a9461eb9e4b352d10fb` | `9682dffc0a1dc4760fb7bb61f6c5d1b8439a7f561e191f9ab5804dd4a9aadc4d` |
| `.gnu.linkonce.this_module` | `0x280` | `0x280` |
| `vermagic` | `6.12.94 SMP mod_unload aarch64` | identical |
| `depends` | `mt76-connac-lib,mt76,mac80211,cfg80211` | identical |

The hardened build adds exactly three undefined symbols and removes none: `__nla_parse`, `nla_put`, and `skb_trim`. All three are present as `EXPORT_SYMBOL` entries in the pinned official SDK `Module.symvers`. The raw module retains the CSI/vendor implementation symbols, including `mt7915_mcu_set_csi`, `mt7915_vendor_csi_ctrl`, `mt7915_vendor_csi_ctrl_dump`, and `mt7915_vendor_register`.

The patch applied cleanly to all eight intended files. The mt76 log ends with `make[1]: Leaving directory` and contains no compiler errors; its only warnings are pre-existing SDK Kconfig type/default warnings and a duplicate Broadcom download rule. The patch input is `0001-mt7915-csi-v2-hardened.patch`, SHA-256 `02d129819a662449ebb443ce5eb6b7bd38db0c99d90cd17aae75e699a9719c3e`.

The vanilla and hardened controls used byte-identical build context:

- SDK `.config`: `ae8da1fb9839fb320e3c5a769c13effc1ad35faa9215d6cb4e2751534186b812`
- kernel `.config`: `0a5f3999395978e021d183ecc3410869b5e97442b20e096110ca5fdc7a56dbb1`
- kernel `Module.symvers`: `c399fe956038c3362d0418267663fdf2ff4cd5dde41f3e182b93964719600139`
- pruned `Config-build.in`: `817304caad383393e3e27c903351b492d5ff74fc04a9c52912fa8786cb4831bc`
- amd64 build image: `ax3000t-openwrt-sdk@sha256:a236c63b8ab01c7d248ca873e952c963ecd2790d74252c50916572294dce8f47`

Complete hardened evidence is generated under `reproduction/out/patched-sdk-control/`; same-context diffs belong under `reproduction/report/patched-sdk-control/`; the deliberate fail-closed live comparison belongs under `reproduction/report/patched-vs-live/`. These directories are intentionally ignored and must not be committed.

## Final safety conclusion

The hardened source compiles and its three new external references resolve in the official SDK context. That proves source compatibility only. Its module still has `.gnu.linkonce.this_module=0x280`, while the installed/public Kwrt module has `0x440`. Its APK still requires `kernel=6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1`, while the installed Kwrt IPK requires `kernel (=6.12.94~1-r1)`. The package formats also differ (official SDK APK versus Kwrt IPK).

Therefore both official-SDK modules and the hardened APK are **fail-closed, absolutely not deployable, and must never be installed on the router or embedded in a release image**. Only an exact pinned Kwrt build can attempt to clear the deployment gate.
