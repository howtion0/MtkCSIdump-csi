#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repro_dir=$(cd -- "$script_dir/.." && pwd)

export MT76_PATCH=${MT76_PATCH:-"$repro_dir/../patches/0001-mt7915-csi-v2-hardened.patch"}
export EXPECTED_MT76_PATCH_SHA256=${EXPECTED_MT76_PATCH_SHA256:-02d129819a662449ebb443ce5eb6b7bd38db0c99d90cd17aae75e699a9719c3e}
export WORK_VOLUME=${WORK_VOLUME:-ax3000t-mt76-csi-hardened-sdk-25125}
export OUTPUT_DIR=${OUTPUT_DIR:-"$repro_dir/out/patched-sdk-control"}

exec "$script_dir/build-vanilla-mt7915e.sh"
