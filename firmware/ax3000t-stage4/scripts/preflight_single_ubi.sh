#!/bin/sh
# Read-only layout audit for a future, separately authorized migration review.
# This script never writes UCI, flash, UBI, mounts, files, or network state.

set -eu

fail() {
	printf 'PRE-FLIGHT FAIL: %s\n' "$1" >&2
	printf '%s\n' 'No flashing is authorized. Never bypass this result with sysupgrade -F.' >&2
	exit 1
}

if [ "$#" -ne 0 ]; then
	fail "this read-only audit accepts no arguments"
fi

board_file=/tmp/sysinfo/board_name
[ -r "$board_file" ] || fail "cannot read board identity at $board_file"
board_name="$(sed -n '1p' "$board_file")"
[ "$board_name" = 'xiaomi,mi-router-ax3000t' ] || \
	fail "board identity is not xiaomi,mi-router-ax3000t"

[ -r /proc/mtd ] || fail "cannot read /proc/mtd"
mtd_lines="$(sed -n '2,$p' /proc/mtd)"
[ -n "$mtd_lines" ] || fail "/proc/mtd has no partitions"

count=0
ubi_mtd=
while IFS=' ' read -r device size erase quoted_name extra; do
	[ -z "${extra:-}" ] || fail "unexpected /proc/mtd field count"
	device="${device%:}"
	name="${quoted_name#\"}"
	name="${name%\"}"
	case "$size$erase" in
		''|*[!0-9a-fA-F]*) fail "non-hexadecimal /proc/mtd size" ;;
	esac
	bytes=$((0x$size))
	erase_bytes=$((0x$erase))
	sysfs="/sys/class/mtd/$device"
	[ -r "$sysfs/offset" ] || fail "cannot read $device partition offset"
	[ -r "$sysfs/name" ] || fail "cannot read $device partition name"
	[ -r "$sysfs/size" ] || fail "cannot read $device partition size"
	[ -r "$sysfs/erasesize" ] || fail "cannot read $device erase size"
	sys_offset_raw="$(sed -n '1p' "$sysfs/offset")"
	case "$sys_offset_raw" in
		0x*)
			hex_offset="${sys_offset_raw#0x}"
			case "$hex_offset" in
				''|*[!0-9a-fA-F]*) fail "non-numeric $device partition offset" ;;
			esac
			;;
		''|*[!0-9]*) fail "non-numeric $device partition offset" ;;
	esac
	sys_offset=$((sys_offset_raw + 0))
	[ "$(sed -n '1p' "$sysfs/name")" = "$name" ] || fail "$device name differs between procfs and sysfs"
	[ "$(sed -n '1p' "$sysfs/size")" -eq "$bytes" ] || fail "$device size differs between procfs and sysfs"
	[ "$(sed -n '1p' "$sysfs/erasesize")" -eq "$erase_bytes" ] || fail "$device erase size differs between procfs and sysfs"
	case "$count:$device:$sys_offset:$bytes:$erase_bytes:$name" in
		0:mtd0:0:1048576:131072:BL2) ;;
		1:mtd1:1048576:262144:131072:Nvram) ;;
		2:mtd2:1310720:262144:131072:Bdata) ;;
		3:mtd3:1572864:2097152:131072:Factory) ;;
		4:mtd4:3670016:2097152:131072:FIP) ;;
		5:mtd5:5767168:262144:131072:crash) ;;
		6:mtd6:6029312:262144:131072:crash_log) ;;
		7:mtd7:123731968:262144:131072:KF) ;;
		8:mtd8:6291456:117440512:131072:ubi) ubi_mtd=8 ;;
		*) fail "MTD index, name, offset, size, or 0x20000 erase size differs from the audited layout" ;;
	esac
	count=$((count + 1))
done <<EOF
$mtd_lines
EOF

[ "$count" -eq 9 ] || fail "expected exactly nine MTD partitions"
[ "$ubi_mtd" = 8 ] || fail "the 112 MiB ubi partition is not mtd8"
grep -q '"ubi_kernel"' /proc/mtd && fail "stock dual-UBI ubi_kernel is still present"

[ -r /sys/class/mtd/mtd8/name ] || fail "cannot read mtd8 sysfs identity"
[ "$(sed -n '1p' /sys/class/mtd/mtd8/name)" = ubi ] || fail "mtd8 sysfs name is not ubi"
[ -r /sys/class/ubi/ubi0/mtd_num ] || fail "ubi0 is not attached or its MTD binding is unreadable"
[ "$(sed -n '1p' /sys/class/ubi/ubi0/mtd_num)" = 8 ] || fail "ubi0 is not attached to mtd8"

command -v ubinfo >/dev/null 2>&1 || fail "ubinfo is unavailable"
ubinfo_output="$(ubinfo -a 2>&1)" || fail "ubinfo could not inspect the attached UBI device"
printf '%s\n' "$ubinfo_output" | grep -q 'ubi0' || fail "ubinfo does not report ubi0"

volume_count=0
for volume in /sys/class/ubi/ubi0_*; do
	[ -d "$volume" ] || fail "ubi0 has no readable volume sysfs entries"
	[ -r "$volume/name" ] || fail "cannot read ${volume##*/} volume name"
	volume_name="$(sed -n '1p' "$volume/name")"
	case "${volume##*/}:$volume_name" in
		ubi0_0:kernel) ;;
		ubi0_1:rootfs) ;;
		ubi0_2:rootfs_data) ;;
		*) fail "UBI volume ID/name set differs from kernel, rootfs, rootfs_data" ;;
	esac
	volume_count=$((volume_count + 1))
done
[ "$volume_count" -eq 3 ] || fail "expected exactly three UBI volumes"

[ -r /proc/mounts ] || fail "cannot read /proc/mounts"
mount_output="$(awk '$2 == "/overlay" { print $1, $3 }' /proc/mounts)" || \
	fail "could not parse UBIFS mounts"
set -- $mount_output
[ "$#" -eq 2 ] || fail "expected exactly one /overlay mount"
[ "$2" = ubifs ] || fail "/overlay is not UBIFS"
case "$1" in
	ubi0:rootfs_data|/dev/ubi0_2) ;;
	*) fail "/overlay source is not the audited ubi0 rootfs_data volume" ;;
esac

printf '%s\n' 'READ-ONLY LAYOUT VERDICT: verified 112 MiB single-UBI runtime.'
printf '%s\n' 'Flashing remains unauthorized; this result does not permit sysupgrade -F.'
