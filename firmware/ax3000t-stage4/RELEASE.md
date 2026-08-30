# GitHub Release checklist

> **EXPERIMENTAL — DO NOT FLASH.** The Release is a reproducibility archive,
> not a deployment instruction. It must say that publication does not authorize
> flashing and link to `RECOVERY.md`.

## Preconditions

- [ ] The build ran in a new work directory from the locked public sources.
- [ ] Two independent fresh-volume builds passed `reproducibility-gates.json`;
      the final signed images and every locked comparison artifact are
      byte-identical.
- [ ] The networked prepare receipts/download manifests pass, and compilation
      occurred only in the recorded `--network=none` phase.
- [ ] `source-pristine-gates.json` is `pass` with no warning/failure.
- [ ] `source-patched-gates.json` is `pass` with no warning/failure.
- [ ] `capture-source-gates.json` reconstructs the exact locked Git tree and
      matches the canonical OpenWrt rawgit `.tar.zst` size, SHA-256 and member
      closure; codeload gzip bytes are not trusted as stable inputs.
- [ ] `vanilla-abi-gates.json` proves IPK, exact kernel dependency,
      `this_module=0x440`, 294 undefined symbols and the locked hash.
- [ ] `gate-report.json` is `pass` with every gate `pass`, including DTB,
      selected FIT payload hashes, exact nine-partition geometry, UBI capacity,
      metadata, generic upgrade path, final-root `platform.sh`/module identity,
      capture package/docs/default-off state, no-preseed Wi-Fi evidence and
      privacy scan.
- [ ] The image has exactly one INFO and one SIGNATURE chunk; `ucert` verifies
      the signed prefix under the independently pinned Stage4 public key, and
      the final rootfs contains that exact public key.
- [ ] `build-provenance.json` says `publication_ready: true`,
      `flash_authorized: false`, and `EXPERIMENTAL-DO-NOT-FLASH`.
- [ ] An independent reviewer has read the diff and the public/private policy.
- [ ] The Release tag's peeled commit is exactly the clean
      `stage4_source_commit` recorded in provenance. A signed annotated tag is
      recommended as an additional source-authenticity check, not a substitute
      for the four-asset byte closure.

## Prepare the only permitted assets

Use a new empty directory:

```sh
python3 scripts/prepare_release_bundle.py \
  --build-output /absolute/path/to/clean-build-output \
  --bundle /absolute/path/to/new-release-bundle
```

The command must emit exactly four files:

```text
ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin
SHA256SUMS
build-provenance.json
gate-report.json
```

Run `sha256sum -c SHA256SUMS` inside the bundle. Never add a file with a
wildcard, never attach the audit directory wholesale, and never include MTD,
Factory, Nvram, Bdata, EEPROM, UBI, calibration or router backup data.

## Release text

The title and first paragraph must contain **EXPERIMENTAL — DO NOT FLASH**.
State the exact OpenWrt/Kwrt/mt76/capture commits, the single-UBI geometry, the
image SHA-256, the gate result, and the precise state: CSI service is disabled;
the image does not preseed or modify Wi-Fi configuration; post-boot/preserved
radio state is not proven by a static image. Do not claim production safety or
location accuracy.

Also state that this archive has **no executable upgrade path**: compat 2.0
blocks stock dual-UBI and older compat-1.0 single-UBI systems, while `-F` is
forbidden. A future layout-aware handoff is a separate reviewed project.

## Upload example — documentation only

After repository review and only from the four-file bundle, a maintainer may
verify a pre-created tag and create a **draft** Release. The exact peeled commit
must equal `stage4_source_commit` in `build-provenance.json`. This draft does
not create or push the tag and does not run these commands:

```sh
stage4_commit="$(git rev-parse HEAD)"
test "$(git status --porcelain)" = ""
test "$(python3 -c 'import json; print(json.load(open("/absolute/path/to/new-release-bundle/build-provenance.json"))["stage4_source_commit"])')" = "$stage4_commit"
test "$(git rev-parse 'stage4-25.12.5-experimental^{commit}')" = "$stage4_commit"

gh release create stage4-25.12.5-experimental \
  --repo howtion0/MtkCSIdump-csi \
  --draft \
  --verify-tag \
  --target "$stage4_commit" \
  --title "EXPERIMENTAL — DO NOT FLASH — AX3000T Stage 4" \
  --notes-file /absolute/path/to/reviewed-release-notes.md \
  /absolute/path/to/new-release-bundle/ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin \
  /absolute/path/to/new-release-bundle/SHA256SUMS \
  /absolute/path/to/new-release-bundle/build-provenance.json \
  /absolute/path/to/new-release-bundle/gate-report.json
```

Do not use `gh release upload directory/*`. Do not upload from a directory that
also contains private recovery material. The example itself neither creates nor
pushes its prerequisite tag. Keep every resulting Release as a draft until a
reviewer independently verifies `SHA256SUMS`, the tag's peeled commit (and its
signature when present), provenance cross-bindings and all four asset names.

The bundler accepts provenance only after two local pinned-container builds. A
GitHub Actions job that merely downloads and re-uploads those bytes must not
call its attestation “build provenance”; it did not build them. A signed
annotated tag and a future independent workflow that genuinely rebuilds the
image may add useful authentication/attestation, but they are enhancements
rather than false claims or extra Release assets. The archive is bound by the
image's fixed-key `usign`/`ucert` signature, `SHA256SUMS`, two-build provenance
and the exact peeled source commit.
