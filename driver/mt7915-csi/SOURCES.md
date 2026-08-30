# Stage 2 source and evidence lock

This directory is intentionally pinned. Replacing any item with a moving
branch changes the evidence and requires the full verification gate again.

| Item | Pinned source | Purpose |
|---|---|---|
| OpenWrt `mt76` baseline | [`39c960c3ada558b4c2e7915772483d3731573d09`](https://github.com/openwrt/mt76/commit/39c960c3ada558b4c2e7915772483d3731573d09) | Exact tree accepted by the hardened patch |
| Original connac2 CSI implementation | MediaTek patch `1001`, author commit `97b4997c04cfdae312ae7a67249422f88be91c40`, carried by [`feed-wifi-master` at `78a4c241`](https://github.com/cmonroe/feed-wifi-master/tree/78a4c241231c93ca045da4368b7c3d8fe904f1dd/mt76/patches) | Vendor ABI and CSI control/event lineage |
| 80 MHz segment evidence | [`mtk-openwrt-feeds` at `511100a8`](https://github.com/mediatek/mtk-openwrt-feeds/tree/511100a886cf99a12588ccbb810c70928a772027/autobuild/unified/filogic/mac80211/25.12/files/package/kernel/mt76/patches) patch `0099-cp-mtk-mt76-mt7915-add-connac2-support.patch` | MT798x/MT7916 First/Middle/Last wire semantics |
| Hardware demonstration | [Nullcon: *Unlock hidden Superpowers in MediaTek Wi-Fi Chips*](https://nullcon.net/wp-content/uploads/2026/04/Unlock-hidden-Superpowers-in-MediaTek-WiFi-Chips.pdf) | Names Xiaomi AX3000T and patched `mt76` CSI path |
| User-space lineage | [MtkWifiRev/MtkCSIdump](https://github.com/MtkWifiRev/MtkCSIdump) | Netlink control and original visualizer |

## Artifact hashes

```text
02d129819a662449ebb443ce5eb6b7bd38db0c99d90cd17aae75e699a9719c3e  patches/0001-mt7915-csi-v2-hardened.patch
fcad08a4c30989c6cc3360275d83524986d773e2e0546ba605ff24d985560d10  patches/reference-nullcon-forward-port.patch
351db16dc70ea486570b3f70fa1e3f1a4637c8054ed4e60a6cfa39d42da32cd1  upstream MediaTek 25.12 patch 0099 used as segment evidence
```

The reference forward-port is retained only for audit comparison. Build inputs
must use `0001-mt7915-csi-v2-hardened.patch`; its extra validation, bounded queue,
optional-field semantics and 80 MHz reassembly are not present in the reference.

All driver files added by these patches keep their upstream SPDX identifiers.
The repository-level license does not erase upstream notices.
