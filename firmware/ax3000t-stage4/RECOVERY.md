# Recovery gate

> **EXPERIMENTAL — DO NOT FLASH.** Passing software checks does not prove that
> a particular router can be recovered. This checklist is a human gate and is
> not automated by the build.

Before anyone considers a lab flash, all boxes must be checked and the answers
recorded privately:

- [ ] The exact hardware is Xiaomi AX3000T with MT7981BA/MT7976CN.
- [ ] A private, verified full partition inventory exists outside Git.
- [ ] Every private backup has a recorded byte size and SHA-256 digest.
- [ ] Factory/Nvram/Bdata/calibration partitions were never edited.
- [ ] UART voltage, ground, RX and TX pins have been identified from trusted
      hardware documentation and tested with read-only boot output.
- [ ] A serial adapter and a second Internet path are available; recovery does
      not depend on the router being flashed.
- [ ] The bootloader recovery/TFTP procedure has been rehearsed with a benign
      read-only or non-writing step.
- [ ] The stock-compatible recovery image and its hash are available locally.
- [ ] The generated candidate has a fully passing `gate-report.json`, including
      final rootfs, ABI, module identity, capture and privacy checks.
- [ ] The local, read-only `scripts/preflight_single_ubi.sh` reports PASS and a
      human confirms `mtd7=KF`, `mtd8=ubi`, `ubi0` attached to `mtd8`, and the
      unique UBI volume mapping `ubi0_0=kernel`, `ubi0_1=rootfs`,
      `ubi0_2=rootfs_data`, with exactly that rootfs_data volume mounted as the
      UBIFS `/overlay`; the output remains in the private experiment log.
- [ ] `SHA256SUMS` was verified immediately before any operation.
- [ ] The operator understands that the 112 MiB single-UBI layout differs from
      the stock dual-UBI layout and will not mix their upgrade paths.
- [ ] Power is stable and no remote/VPN session is required to finish recovery.
- [ ] A human reviewer has explicitly changed the private experiment record
      from “not authorized” to “authorized for this one device.”

Never treat `sysupgrade -F` as a migration step. A first migration from stock
dual-UBI is a separate future procedure requiring explicit authorization,
working UART/recovery, an offline second network path and human review; this
Stage 4 Release does not define or authorize that migration.

The same is true for an older compat-1.0 112 MiB single-UBI installation:
Stage4 compat 2.0 deliberately blocks ordinary sysupgrade, and this archive
does not provide a reviewed compatibility handoff. Therefore the current
Release has no executable upgrade path at all.

Do not put completed checklist details, serial output, MAC addresses, partition
tables read from the device, or backup locations in a public issue or Release.

The build provenance intentionally keeps `flash_authorized: false`. Publishing
an experimental asset for reproducibility/backup does not change that field.
