# Stage4 public signing inputs

This directory contains the dedicated Stage4 public signing identity. It is
public build input, not authorization to flash an image.

Only these two public, immutable identity files belong here:

- `ax3000t-stage4.pub`
- `ax3000t-stage4.ucert`

Their SHA-256 values, validity interval, and `usign -F` fingerprint are locked
in `source-lock.json`. The private file named `ax3000t-stage4` must stay
outside the repository, output tree, Docker volume, logs, and GitHub Release.
It is supplied only through the `STAGE4_SIGNING_KEY_FILE` secret-path interface.

The base ucert is locked as bytes because `ucert -I` records wall-clock validity
times. Regenerating it inside each clean build would make otherwise identical
images differ.

## Maintainer generation procedure

Use the `usign` and `ucert` host tools built from the commits pinned in
`source-lock.json`. Generate every key/certificate/revocation artifact in one
external mode-0700 secret directory, with networking disabled and the
linux/amd64 container root read-only. Mount the pinned host-tools tree
read-only; mount only the external secret directory writable. For example,
substitute audited absolute paths and the pinned builder image digest below:

```sh
secret_dir=/absolute/path/outside-repository/ax3000t-stage4-secrets
install -d -m 0700 "$secret_dir"
docker run --rm --platform linux/amd64 --network=none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m \
  --mount type=bind,src=/absolute/path/to/pinned-host-tools,dst=/tools,readonly \
  --mount type=bind,src="$secret_dir",dst=/secret \
  ax3000t-stage4-builder@sha256:PINNED_IMAGE_DIGEST \
  sh -eu -c 'PATH=/tools/bin:$PATH; umask 077; \
    usign -G -s /secret/ax3000t-stage4 \
      -p /secret/ax3000t-stage4.pub -c "AX3000T Stage4 release key"; \
    ucert -I -c /secret/ax3000t-stage4.ucert \
      -p /secret/ax3000t-stage4.pub -s /secret/ax3000t-stage4; \
    chmod 0600 /secret/ax3000t-stage4; \
    chmod 0644 /secret/ax3000t-stage4.pub /secret/ax3000t-stage4.ucert'

install -m 0644 "$secret_dir/ax3000t-stage4.pub" keys/ax3000t-stage4.pub
install -m 0644 "$secret_dir/ax3000t-stage4.ucert" keys/ax3000t-stage4.ucert
```

For key rotation only, compare `usign -F -s /private/ax3000t-stage4` with
`usign -F -p keys/ax3000t-stage4.pub`; they must be identical. Record only that
lowercase fingerprint, the certificate validity interval, and the SHA-256
values of the two public files in `source-lock.json`, then set signing status
to `READY`. Never record the private key hash: even a hash is unnecessary
private-key metadata. `ucert -I` also creates revocation material; it stays in
the external secret directory and must never be copied into Git or a Release.
The source gates actively reject private/revocation material even if ignore
rules would hide it.

The locked base certificate is valid only from Unix time `1788126829` through
`1819662829` (one year). Every signed build verifies and records that exact
window. Expiry requires a separately reviewed key/certificate rotation; do not
silently regenerate the certificate during a build.

The supported build interface remains:

```sh
STAGE4_SIGNING_KEY_FILE=/private/ax3000t-stage4 JOBS=2 \
  scripts/run_repro_pair.sh
```

The wrapper requires the private file to be a non-symlink regular file with
mode `0600`, mounts it read-only into only the network-disabled phase, verifies
its fingerprint, copies it into a 1 MiB tmpfs as OpenWrt's `BUILD_KEY` prefix,
and removes the tmpfs copy on exit.
