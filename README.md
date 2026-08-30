# MtkCSIdump · trustworthy MediaTek CSI capture

MtkCSIdump turns the CSI reports produced by a patched MediaTek `mt76` driver
into a documented, portable UDP stream. Its progressive branches preserve the
capture foundation, harden the AX3000T driver contract, and add an evidence-
gated coarse-localization layer without pretending that two antennas are a
camera. Raw I/Q, real RX/TX identities, timing, and PPDU metadata remain
available at every later stage.

![Original MtkCSIdump visualizer](https://raw.githubusercontent.com/MtkWifiRev/MtkCSIdump/refs/heads/main/csi_demo.gif)

> **Honesty boundary:** this stage captures measurements; it does not claim that
> a single router already produces a human mesh, exact distance, or calibrated
> angle. Direction and range require antenna calibration, packet pairing, a
> stable transmitter, and algorithms built on top of this data.

## Progressive GitHub lines

| Branch | Deliverable | Safety state |
|---|---|---|
| `codex/stage-1-capture` | Portable CSI2 capture, parser and GUI | Hardware-free tests passed |
| `codex/stage-2-driver` | [Hardened MT7915 CSI patch and frozen ABI](driver/mt7915-csi/README.md) | Source/ABI gate only; not loaded on a router |
| `codex/stage-3-localization` | [Coarse AoA, range proxy, relative CIR and multi-receiver fusion](localization/README.md) | Offline/synthetic gates only; real accuracy remains unmeasured |

Each line is usable as an audit checkpoint. A green source test does not imply
that a module or firmware image is safe to deploy; deployment has separate
kernel ABI, flash-layout, Ethernet and rollback gates.

## What is fixed in Stage 1

- **No more fixed 61-point output.** Legacy Nullcon `mt76` reports omit
  `data_num`, so MtkCSIdump now derives the valid FFT span from `data_bw`:
  64 / 128 / 256 points for 20 / 40 / 80 MHz. The Stage-2 ABI's explicit
  `data_num` is preferred when present.
- **No three-antenna assumption.** Every report is emitted once with its native
  16-bit `rx_idx` and `tx_idx`. The visualizer creates chain views on demand.
- **Both known kernel ABIs are understood.** The decoder accepts the proven
  Nullcon layout, MediaTek's shifted extended layout, and the append-only Stage-2
  metadata layout without guessing attribute widths.
- **Malformed netlink messages fail closed.** Required attributes and integer
  widths are checked, I and Q lengths must agree, all structures are initialized,
  partial requests are discarded, and CSI enable failures trigger a best-effort
  disable rollback.
- **UDP v2 is portable.** It uses network byte order and signed IQ16 samples,
  with driver/host time, transmitter MAC, RSSI/SNR, bandwidth, chain IDs,
  `h_idx`, `chain_info`, `pkt_sn`, segment state, and an explicit presence map.
- **The GUI remains backward compatible.** It reads both the old native-layout
  UDP v1 datagrams and the new v2 stream. The server emits v2 only.

## AX3000T interpretation

The Xiaomi AX3000T (MT7981 + MT7976) is a 2×2 MIMO design on 2.4 GHz and 5 GHz.
That means at most two simultaneously useful receive elements per band for this
pipeline—not four synchronized array elements, and not one element per distinct
numeric `rx_idx` ever observed. MtkCSIdump reports the firmware labels; it does
not rename a chain to “left” or “right”. The usable pair and its physical RF/
antenna mapping belong in a measured calibration file.

For localization work, preserve packets sharing this identity when available:

```text
transmitter MAC + band + driver timestamp + pkt_sn
```

`h_idx` and `chain_info` are useful metadata, but neither is a reliable
substitute for `pkt_sn` when pairing multiple receive-chain observations from
one PPDU.

The Stage-2 kernel ABI reassembles segmented 80 MHz firmware reports before
netlink delivery: First/Middle reports are held in-kernel and only the completed
Last report is exported as one `data_num=256` record. Its `segment_num` is the
final firmware segment index and `remain_last` is zero. Keep those fields as
provenance; **do not concatenate or reassemble the I/Q payload again in user
space**.

## Stage 3: honest coarse localization

Stage 3 consumes only CSI2 records that pass the Stage-2 frequency, bandwidth,
tone-order and segment gates. It then provides four deliberately bounded
outputs:

- same-PPDU pairing on the full `(tx_idx, rx_idx, transport_stream)` identity;
- calibrated two-element angle **support** with 13 or more display sectors,
  front/back ambiguity and grating-lobe candidates kept visible;
- a room/device-labelled near/mid/far proxy, never CSI absolute distance;
- relative CIR diagnostics and, with physically separate synchronized
  receivers, a two-dimensional normalized-support heatmap.

A single AX3000T is therefore useful for experimentally showing left/centre/
right or finer sector support after measuring its antenna mapping and fixed
chain phase. It is not enough for an unambiguous 360-degree bearing, absolute
ToF range, centimetre coordinates, a body outline, or a RuView-style human
mesh. Start with [the Stage-3 guide](localization/README.md), then read the
[physical boundary table](localization/BOUNDARIES.md) before collecting data.

The calibration is deliberately two-capture: a signed-angle fit capture plus
an independent opposite-side holdout. Unknown CSI2 semantics, inferred BW/tone
count, unknown `rx_mode`, a mode change, repeated capture artifacts, or a
cross-angle phase residual above the hard gate all stop analysis. A range proxy
can influence 2-D fusion only when its feature window is bound to the same
capture-manifest content ID, receiver, TA, radio profile and overlapping actual
record window as the AoA observation.

The deterministic software demo is explicitly synthetic:

![AX3000T CSI algorithm families and boundaries](localization/ALGORITHM_BOUNDARY_MAP.svg)

```bash
uv sync --extra test --frozen
uv run --frozen pytest
uv run --frozen python -m localization.cli demo --output-dir synthetic-demo
uv run --frozen python -m build
uv run --frozen python tools/verify_stage3_sdist.py \
  dist/ax3000t_csi_localization-0.1.0.tar.gz
```

Real captures, session manifests, room maps and calibration artifacts are
ignored by default. The recorder creates no-clobber private `0600` capture and
manifest files; the source distribution is checked against an exact public
allowlist rather than recursive globs. See [the privacy policy](localization/PRIVACY.md).

## Build

The repository no longer embeds one developer's compiler or SDK path. Pass the
toolchain and `libnl-tiny` locations explicitly:

```bash
cmake -S . -B build \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/openwrt-toolchain.cmake \
  -DLIBNL_TINY_INCLUDE_DIR=/path/to/staging/usr/include/libnl-tiny \
  -DLIBNL_TINY_LIBRARY=/path/to/staging/usr/lib/libnl-tiny.so \
  -DBUILD_TESTING=OFF
cmake --build build --parallel
```

For a static target, add `-DCSI_STATIC_LINK=ON` only when the SDK contains all
required static libraries. Dynamic linking is the safer default on OpenWrt.

### Hardware-free verification

The protocol and parser tests do not need an AX3000T or `libnl-tiny`:

```bash
cmake -S . -B build-host -DCSI_BUILD_CAPTURE=OFF -DBUILD_TESTING=ON
cmake --build build-host --parallel
ctest --test-dir build-host --output-on-failure
```

They cover 40/80 MHz point counts, arbitrary RX chain IDs, signed network-order
I/Q, v1 compatibility, PPDU metadata, malformed lengths, and oversize rejection.

## Run without disturbing the management network

Keep router management on Ethernet and keep the computer's normal Wi-Fi/VPN as
its default route. MtkCSIdump itself does not modify routes, DNS, firewall rules,
SSIDs, or radio channel settings.

On the router:

```bash
./CSIdump <wireless-interface> <poll-ms> <udp-port>
```

Example:

```bash
./CSIdump phy1-ap0 100 8888
```

On the visualization computer:

```bash
python3 -m pip install -r requirements.txt
python3 csi_udp_client_gui.py 192.0.2.1 8888
```

`192.0.2.1` is a documentation-only address; replace it with the router's
management address while keeping the Mac's normal Wi-Fi/VPN as the default
route.

The GUI sends a small `register` datagram; the router then streams one CSI v2
datagram per firmware observation. `Ctrl+C` disables CSI capture and closes the
UDP service. Bind/firewall the port to a trusted management LAN—v2 contains
transmitter MAC addresses and raw radio measurements. The server accepts at most
16 distinct UDP registrations per run so unauthenticated LAN traffic cannot grow
the client list without bound.

## Wire format and missing data

The exact 80-byte header and presence flags are specified in
[docs/UDP_V2.md](docs/UDP_V2.md). A zero is never silently interpreted as
“missing” for optional PPDU fields: the presence bitmap decides. MCS/NSS/rate
remain deliberately absent because the current driver ABI does not export a
validated value. `rx_mode` is retained under its actual name; it is a PHY receive
mode enum, not an MCS field. Stage 3 accepts only enum values handled by the
audited type-5 mask/reorder switch and binds each to an explicit tone profile;
it does not infer an unknown mode.

## Current boundary

- Legacy CSI is limited to 256 complex points; it cannot faithfully transport a
  512-point 160 MHz report.
- UDP v2 accepts at most 1024 complex points and rejects anything larger before
  `sendto`. A 1024-point packet is well below the UDP payload ceiling, although
  it may still be fragmented by a normal 1500-byte Ethernet MTU.
- Host time is wall-clock nanoseconds captured after the netlink batch arrives;
  driver time is the firmware-provided 32-bit counter. They are not the same
  clock and must be aligned by downstream code.
- Channel center frequency is reserved in v2 and is zero unless an authoritative
  source supplies it. The capture executable queries the interface with the
  read-only `NL80211_CMD_GET_INTERFACE` request for every dump batch, preferring
  `CENTER_FREQ1` and marking a `WIPHY_FREQ` primary-channel fallback in the
  quality flags. The reported nl80211 width must also agree with a non-inferred
  CSI `ch_bw`; a frequency or width change before/after a dump discards that
  batch. A change detected between polls also drains and discards the first
  batch of the new radio epoch, preventing queued old-channel records from
  being mislabeled. Capture startup
  also requests the driver's type-5 tone mask+reorder mode and marks successful
  records, so localization code can refuse unknown carrier ordering.
  `pri_ch_idx` is not an IEEE channel number.
- Raw inter-chain phase is not a calibrated AoA. Fixed RF-chain offsets and the
  real antenna geometry must be measured before MUSIC/ESPRIT or learned angle
  models can be trusted.

## Lineage

This work builds on
[MtkWifiRev/MtkCSIdump](https://github.com/MtkWifiRev/MtkCSIdump) and
[LukasVirecGL/meta-gl-motion-detection](https://github.com/LukasVirecGL/meta-gl-motion-detection).
Keep the project license and upstream attribution when redistributing binaries
or firmware images.
