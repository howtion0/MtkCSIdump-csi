#!/usr/bin/env bash
set -euo pipefail

expected_bytes=252397003
expected_sha256=ff4a38a397caa2cfe1c39e18f84ddede14878221b3593c3f2c4cfe24e3ec4c25

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

if [[ $# -ne 1 ]]; then
    echo "usage: $0 /absolute/path/to/official-openwrt-sdk-25.12.5.tar.zst" >&2
    exit 2
fi

sdk_archive=$1
if [[ ! -f "$sdk_archive" ]]; then
    echo "SDK archive does not exist: $sdk_archive" >&2
    exit 1
fi

actual_bytes=$(wc -c < "$sdk_archive" | tr -d ' ')
actual_sha256=$(sha256_file "$sdk_archive")

printf 'SDK: %s\n' "$sdk_archive"
printf 'bytes: %s\n' "$actual_bytes"
printf 'sha256: %s\n' "$actual_sha256"

if [[ "$actual_bytes" != "$expected_bytes" ]]; then
    echo "size mismatch: expected $expected_bytes bytes" >&2
    exit 1
fi

if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    echo "SHA-256 mismatch: expected $expected_sha256" >&2
    exit 1
fi

echo "verified: OpenWrt v25.12.5 MediaTek Filogic SDK"
