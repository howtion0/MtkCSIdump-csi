# Public versus private artifact policy

This boundary is absolute: a source-built generic image may be public; content
read from a physical router may not be public.

## Allowed public material

- source code, build scripts, patches and documentation;
- public upstream commit/tree/archive hashes;
- a clean generic sysupgrade image generated from those sources;
- sanitized gate reports and provenance containing basenames, sizes and hashes;
- `SHA256SUMS` for the explicit release asset set.
- in Git only, the dedicated Stage4 **public** signing key/base ucert and their
  hashes; the exact four-file Release does not add them as separate assets,
  while the image/provenance bind their identity. The corresponding private
  key is never public or copied into a retained build volume.

## Never allowed

- raw or decoded `mtd*` partitions;
- Factory, Nvram, Bdata, EEPROM, ART or calibration content;
- NAND/UBI dumps, bootloader backups, sysupgrade backups or rollback archives;
- router-derived MAC addresses, serial numbers, Wi-Fi credentials, passwords,
  SSH keys, router/device certificates or user configuration;
- local private backup paths or filenames in public JSON/logs;
- any binary copied from the running router merely because it appears generic.

Private recovery backups stay outside this draft and outside every Git worktree.
They must not be renamed to resemble a build artifact, copied into `out/`, or
attached to an issue for debugging.

## Enforcement

The final image verifier extracts the rootfs and rejects credentials, private
keys, device-like MAC addresses and backup paths. Public reports contain only
basenames, not local absolute paths.

`scripts/prepare_release_bundle.py` uses a fixed allowlist and never a wildcard.
It creates exactly these four assets:

1. `ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin`
2. `SHA256SUMS`
3. `build-provenance.json`
4. `gate-report.json`

The script also requires passing source, vanilla-ABI and final-image reports,
checks every hash, rejects private-artifact filename patterns, and scans the
public files for local/private path markers. A manually assembled Release is
outside the verified process.

If a questionable file is found, stop. Do not inspect or upload it from this
repository; identify it in the private recovery inventory instead.
