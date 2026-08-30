#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repro_dir=$(cd -- "$script_dir/.." && pwd)

: "${SOURCE_ROOT:?set SOURCE_ROOT to the pinned OpenWrt v25.12.5 checkout}"
: "${SDK_ARCHIVE:?set SDK_ARCHIVE to the verified OpenWrt v25.12.5 Filogic SDK archive}"

source_root=$(cd -- "$SOURCE_ROOT" && pwd)
sdk_archive=$SDK_ARCHIVE
container_image=${CONTAINER_IMAGE:-ax3000t-openwrt-sdk:22.04}
work_volume=${WORK_VOLUME:-ax3000t-mt76-vanilla-25125}
output_dir=${OUTPUT_DIR:-"$repro_dir/out/vanilla"}
sdk_name=openwrt-sdk-25.12.5-mediatek-filogic_gcc-14.3.0_musl.Linux-x86_64
mt76_patch=${MT76_PATCH:-}
expected_patch_sha256=${EXPECTED_MT76_PATCH_SHA256:-}
expected_source_commit=f0a60eee2fe051741c643ea6118718aae1ef17fb
expected_mt76_commit=39c960c3ada558b4c2e7915772483d3731573d09
expected_mt76_mirror_hash=7a9f8ea21eee5324e6638ace627dd305b3650ae6ca86109317d9ee83702140eb

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        echo "neither sha256sum nor shasum is available" >&2
        return 1
    fi
}

"$script_dir/verify-sdk.sh" "$sdk_archive"

if ! git -C "$source_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "SOURCE_ROOT is not a Git checkout: $source_root" >&2
    exit 1
fi

actual_source_commit=$(git -C "$source_root" rev-parse HEAD)
if [[ "$actual_source_commit" != "$expected_source_commit" ]]; then
    echo "OpenWrt source commit mismatch" >&2
    echo "expected: $expected_source_commit" >&2
    echo "actual:   $actual_source_commit" >&2
    exit 1
fi

for source_dir in \
    "$source_root/package/kernel/mt76" \
    "$source_root/package/kernel/mac80211" \
    "$source_root/package/libs/libnl-tiny"; do
    if [[ ! -d "$source_dir" ]]; then
        echo "required source directory is missing: $source_dir" >&2
        exit 1
    fi
done

source_status=$(git -C "$source_root" status --porcelain --untracked-files=all -- \
    package/kernel/mt76 \
    package/kernel/mac80211 \
    package/libs/libnl-tiny)
if [[ -n "$source_status" ]]; then
    echo "required package inputs are not clean at the pinned commit:" >&2
    printf '%s\n' "$source_status" >&2
    exit 1
fi

mt76_makefile=$source_root/package/kernel/mt76/Makefile
grep -Fqx "PKG_SOURCE_VERSION:=$expected_mt76_commit" "$mt76_makefile" || {
    echo "mt76 source revision is not pinned to $expected_mt76_commit" >&2
    exit 1
}
grep -Fqx "PKG_MIRROR_HASH:=$expected_mt76_mirror_hash" "$mt76_makefile" || {
    echo "mt76 mirror hash mismatch" >&2
    exit 1
}

if docker volume inspect "$work_volume" >/dev/null 2>&1; then
    echo "refusing to overwrite Docker volume: $work_volume" >&2
    echo "choose a fresh WORK_VOLUME name; this script never deletes build state" >&2
    exit 1
fi

if [[ -e "$output_dir" ]] && [[ -n "$(find "$output_dir" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    echo "refusing to overwrite non-empty output directory: $output_dir" >&2
    exit 1
fi

patch_mount=()
patch_copy=
if [[ -n "$mt76_patch" ]]; then
    if [[ ! -f "$mt76_patch" ]]; then
        echo "mt76 patch does not exist: $mt76_patch" >&2
        exit 1
    fi
    if [[ -z "$expected_patch_sha256" ]]; then
        echo "EXPECTED_MT76_PATCH_SHA256 is required when MT76_PATCH is set" >&2
        exit 1
    fi
    actual_patch_sha256=$(sha256_file "$mt76_patch")
    if [[ "$actual_patch_sha256" != "$expected_patch_sha256" ]]; then
        echo "mt76 patch SHA-256 mismatch" >&2
        echo "expected: $expected_patch_sha256" >&2
        echo "actual:   $actual_patch_sha256" >&2
        exit 1
    fi
    patch_mount=(-v "$mt76_patch:/patch-input/999-mt7915-csi-v2-hardened.patch:ro")
    patch_copy='mkdir -p "$sdk/package/kernel/mt76/patches"; cp /patch-input/999-mt7915-csi-v2-hardened.patch "$sdk/package/kernel/mt76/patches/999-mt7915-csi-v2-hardened.patch"'
fi

docker build --platform linux/amd64 -t "$container_image" "$repro_dir/container"
docker volume create "$work_volume" >/dev/null
mkdir -p "$output_dir"

sdk_parent=$(cd -- "$(dirname -- "$sdk_archive")" && pwd)
sdk_file=$(basename -- "$sdk_archive")

# The archive and pinned source checkout are read-only inputs. Config-build.in is
# pruned only to avoid building every default=m SDK package; the kernel config and
# Module.symvers supplied by the official SDK are not changed.
docker run --platform linux/amd64 --rm \
    -v "$work_volume:/work" \
    -v "$sdk_parent:/input:ro" \
    -v "$source_root/package:/source-package:ro" \
    "${patch_mount[@]}" \
    "$container_image" bash -lc "
set -euo pipefail
tar --zstd -xf /input/$sdk_file -C /work
sdk=/work/$sdk_name
cp -a /source-package/kernel/mt76 \"\$sdk/package/kernel/mt76\"
cp -a /source-package/kernel/mac80211 \"\$sdk/package/kernel/mac80211\"
mkdir -p \"\$sdk/package/libs\"
cp -a /source-package/libs/libnl-tiny \"\$sdk/package/libs/libnl-tiny\"
$patch_copy
cd \"\$sdk\"
make defconfig > /work/01-defconfig-initial.log 2>&1
cp Config-build.in /work/Config-build.original.in
sed -E 's/^([[:space:]]*)default m$/\\1default n/' Config-build.in > /work/Config-build.pruned.in
mv /work/Config-build.pruned.in Config-build.in
cp Config-build.in /work/Config-build.pruned.in
sed -E \
    -e 's/^(CONFIG_PACKAGE_[^=]+)=[ym]$/# \\1 is not set/' \
    -e 's/^(CONFIG_ALL(_KMODS|_NONSHARED)?)=[ym]$/# \\1 is not set/' \
    -e 's/^(CONFIG_TARGET_ALL_PROFILES)=[ym]$/# \\1 is not set/' \
    .config > .config.pruned
mv .config.pruned .config
printf '%s\\n' 'CONFIG_PACKAGE_kmod-mt7915e=m' >> .config
make defconfig > /work/02-defconfig-pruned.log 2>&1
cp .config /work/sdk.config
"

# Deliberately use -j1 V=s. The parallel top-level SDK build can hide the first
# failing dependency; this command gives a complete, deterministic audit log.
docker run --platform linux/amd64 --rm \
    -v "$work_volume:/work" \
    -w "/work/$sdk_name" \
    "$container_image" bash -lc '
set -euo pipefail
make package/kernel/mt76/compile -j1 V=s > /work/03-mt76-verbose.log 2>&1
'

docker run --platform linux/amd64 --rm \
    -v "$work_volume:/work:ro" \
    -v "$output_dir:/export" \
    "$container_image" bash -lc "
set -euo pipefail
sdk=/work/$sdk_name
raw=\$(find \"\$sdk/build_dir\" -type f -path '*/.pkgdir/kmod-mt7915e/lib/modules/6.12.94/mt7915e.ko' -print -quit)
apk=\$(find \"\$sdk/bin/targets/mediatek/filogic/packages\" -maxdepth 1 -type f -name 'kmod-mt7915e-*.apk' -print -quit)
test -n \"\$raw\"
test -n \"\$apk\"
mkdir -p /tmp/apk-extract
\"\$sdk/staging_dir/host/bin/apk\" extract --allow-untrusted --destination /tmp/apk-extract \"\$apk\"
packaged=\$(find /tmp/apk-extract -type f -path '*/lib/modules/6.12.94/mt7915e.ko' -print -quit)
test -n \"\$packaged\"
cp \"\$raw\" /export/mt7915e.raw.ko
cp \"\$packaged\" /export/mt7915e.packaged.ko
cp \"\$apk\" /export/
cp /work/sdk.config /export/
cp /work/Config-build.pruned.in /export/
cp /work/01-defconfig-initial.log /export/
cp /work/02-defconfig-pruned.log /export/
cp /work/03-mt76-verbose.log /export/
if [[ -f \"\$sdk/package/kernel/mt76/patches/999-mt7915-csi-v2-hardened.patch\" ]]; then
    cp \"\$sdk/package/kernel/mt76/patches/999-mt7915-csi-v2-hardened.patch\" /export/
fi
cp \"\$sdk/build_dir/target-aarch64_cortex-a53_musl/linux-mediatek_filogic/linux-6.12.94/.config\" /export/kernel-6.12.94.config
cp \"\$sdk/build_dir/target-aarch64_cortex-a53_musl/linux-mediatek_filogic/linux-6.12.94/Module.symvers\" /export/kernel-6.12.94.Module.symvers
\"\$sdk/staging_dir/host/bin/apk\" adbdump \"\$apk\" > /export/package.adbdump.txt
cd /export
sha256sum \
    Config-build.pruned.in \
    kernel-6.12.94.Module.symvers \
    kernel-6.12.94.config \
    kmod-mt7915e-*.apk \
    mt7915e.packaged.ko \
    mt7915e.raw.ko \
    sdk.config > SHA256SUMS
"

echo "SDK control outputs: $output_dir"
echo "persistent build volume: $work_volume"
echo "SAFETY: official-SDK artifacts are compile controls, not deployment packages"
