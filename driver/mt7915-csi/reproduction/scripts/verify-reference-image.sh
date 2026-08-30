#!/usr/bin/env bash
set -euo pipefail

expected_bytes=40284445
expected_sha256=fdb0f654bdc5a804c296a23b6446dfb08d20bc597ac220d30800a24ce0b37e07
expected_module_bytes=218088
expected_module_sha256=346ab2d4ddcd26322c6f00f85f1c2567a722d9bc605d7ee2e0084af3a64b9621
container_image=${CONTAINER_IMAGE:-ax3000t-openwrt-sdk:22.04}

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

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 public-07.15.2026-sysupgrade.bin [saved-live-mt7915e.ko]" >&2
    exit 2
fi

image_path=$1
live_module=${2:-}

if [[ ! -f "$image_path" ]]; then
    echo "reference image does not exist: $image_path" >&2
    exit 1
fi

actual_bytes=$(wc -c < "$image_path" | tr -d ' ')
actual_sha256=$(sha256_file "$image_path")

printf 'image_bytes=%s\n' "$actual_bytes"
printf 'image_sha256=%s\n' "$actual_sha256"

[[ "$actual_bytes" == "$expected_bytes" ]] || {
    echo "reference image size mismatch" >&2
    exit 1
}
[[ "$actual_sha256" == "$expected_sha256" ]] || {
    echo "reference image SHA-256 mismatch" >&2
    exit 1
}

image_parent=$(cd -- "$(dirname -- "$image_path")" && pwd)
image_file=$(basename -- "$image_path")

docker_args=(
    --platform linux/amd64
    --rm
    -i
    -v "$image_parent:/reference:ro"
)

if [[ -n "$live_module" ]]; then
    [[ -f "$live_module" ]] || {
        echo "saved live module does not exist: $live_module" >&2
        exit 1
    }
    live_parent=$(cd -- "$(dirname -- "$live_module")" && pwd)
    live_file=$(basename -- "$live_module")
    docker_args+=( -v "$live_parent:/live:ro" )
else
    live_file=
fi

docker run "${docker_args[@]}" "$container_image" bash -s -- \
    "$image_file" "$live_file" "$expected_module_bytes" "$expected_module_sha256" <<'CONTAINER'
set -euo pipefail

image=/reference/$1
live_file=$2
expected_module_bytes=$3
expected_module_sha256=$4
prefix=sysupgrade-xiaomi_mi-router-ax3000t

mapfile -t entries < <(tar -tf "$image")
expected_entries=(
    "$prefix/"
    "$prefix/CONTROL"
    "$prefix/kernel"
    "$prefix/root"
)
[[ "${entries[*]}" == "${expected_entries[*]}" ]] || {
    printf 'unexpected sysupgrade entries:\n%s\n' "${entries[*]}" >&2
    exit 1
}

control=$(tar -xOf "$image" "$prefix/CONTROL")
[[ "$control" == 'BOARD=xiaomi_mi-router-ax3000t' ]]

tar -xOf "$image" "$prefix/kernel" > /tmp/kernel
tar -xOf "$image" "$prefix/root" > /tmp/root

printf 'kernel_bytes=%s\n' "$(stat -c %s /tmp/kernel)"
printf 'kernel_sha256=%s\n' "$(sha256sum /tmp/kernel | awk '{print $1}')"
printf 'root_bytes=%s\n' "$(stat -c %s /tmp/root)"
printf 'root_sha256=%s\n' "$(sha256sum /tmp/root | awk '{print $1}')"

metadata=$(tail -c 4096 "$image" | strings | grep '"metadata_version"')
grep -Fq '"supported_devices":["xiaomi,mi-router-ax3000t"]' <<<"$metadata"
grep -Fq '"revision": "07.15.2026"' <<<"$metadata"
grep -Fq '"target": "mediatek/filogic"' <<<"$metadata"
printf 'fwtool_metadata=%s\n' "$metadata"

fdtget -t bx /tmp/kernel /images/fdt-1 data \
    | perl -ne 'for (split) { print pack("C", hex($_)) }' \
    > /tmp/board.dtb

partition=/soc/spi@1100a000/flash@0/partitions/partition@600000
[[ "$(fdtget -t s /tmp/board.dtb "$partition" label)" == 'ubi' ]]
[[ "$(fdtget -t x /tmp/board.dtb "$partition" reg)" == '600000 7000000' ]]
if dtc -I dtb -O dts /tmp/board.dtb 2>/dev/null | grep -Fq 'label = "ubi_kernel"'; then
    echo "embedded DTB unexpectedly contains ubi_kernel" >&2
    exit 1
fi
printf 'dtb_compatible=%s\n' "$(fdtget -t s /tmp/board.dtb / compatible)"
echo 'dtb_partition=ubi@0x00600000+0x07000000'

unsquashfs -cat /tmp/root lib/upgrade/platform.sh > /tmp/platform.sh 2>/dev/null
awk '
    /^platform_do_upgrade\(\)/ { inside=1 }
    inside { print }
    inside && /^}/ { exit }
' /tmp/platform.sh > /tmp/platform-do-upgrade.sh

if grep -Fq 'xiaomi,mi-router-ax3000t|' /tmp/platform-do-upgrade.sh; then
    echo "stock AX3000T unexpectedly has a special upgrade branch" >&2
    exit 1
fi
if grep -Fq 'CI_KERN_UBIPART' /tmp/platform-do-upgrade.sh; then
    echo "upgrade function unexpectedly assigns CI_KERN_UBIPART" >&2
    exit 1
fi
grep -Fq 'nand_do_upgrade "$1"' /tmp/platform-do-upgrade.sh
echo 'upgrade_path=generic-nand_do_upgrade'

unsquashfs -cat /tmp/root lib/modules/6.12.94/mt7915e.ko \
    > /tmp/reference-mt7915e.ko 2>/dev/null
module_bytes=$(stat -c %s /tmp/reference-mt7915e.ko)
module_sha256=$(sha256sum /tmp/reference-mt7915e.ko | awk '{print $1}')
[[ "$module_bytes" == "$expected_module_bytes" ]]
[[ "$module_sha256" == "$expected_module_sha256" ]]
printf 'reference_mt7915e_bytes=%s\n' "$module_bytes"
printf 'reference_mt7915e_sha256=%s\n' "$module_sha256"

if [[ -n "$live_file" ]]; then
    cmp -s /tmp/reference-mt7915e.ko "/live/$live_file"
    echo 'reference_vs_saved_live=byte-identical'
fi
CONTAINER

echo "verified: Kwrt 07.15.2026 public AX3000T single-UBI reference"
