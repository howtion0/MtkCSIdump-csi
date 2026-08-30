#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 live-mt7915e.ko reproduced-mt7915e.ko report-directory" >&2
    exit 2
fi

live_module=$1
reproduced_module=$2
report_dir=$3
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

for module in "$live_module" "$reproduced_module"; do
    if [[ ! -f "$module" ]]; then
        echo "module does not exist: $module" >&2
        exit 1
    fi
done

mkdir -p "$report_dir"

live_parent=$(cd -- "$(dirname -- "$live_module")" && pwd)
live_file=$(basename -- "$live_module")
repro_parent=$(cd -- "$(dirname -- "$reproduced_module")" && pwd)
repro_file=$(basename -- "$reproduced_module")

: > "$report_dir/SHA256SUMS"
for module in "$live_module" "$reproduced_module"; do
    printf '%s  %s\n' "$(sha256_file "$module")" "$module" >> "$report_dir/SHA256SUMS"
done
wc -c "$live_module" "$reproduced_module" > "$report_dir/SIZES.txt"

docker run --platform linux/amd64 --rm \
    -v "$live_parent:/live:ro" \
    -v "$repro_parent:/repro:ro" \
    -v "$report_dir:/report" \
    "$container_image" bash -lc "
set -euo pipefail
readelf -p .modinfo /live/$live_file > /report/live.modinfo.txt
readelf -p .modinfo /repro/$repro_file > /report/reproduced.modinfo.txt
readelf -Ws /live/$live_file | awk '\$7 == \"UND\" && \$8 != \"\" {print \$8}' | sort -u > /report/live.undefined.txt
readelf -Ws /repro/$repro_file | awk '\$7 == \"UND\" && \$8 != \"\" {print \$8}' | sort -u > /report/reproduced.undefined.txt
readelf -Ws /live/$live_file | awk '\$7 != \"UND\" && \$4 == \"FUNC\" {print \$8}' | sort -u > /report/live.defined-functions.txt
readelf -Ws /repro/$repro_file | awk '\$7 != \"UND\" && \$4 == \"FUNC\" {print \$8}' | sort -u > /report/reproduced.defined-functions.txt
readelf -SW /live/$live_file > /report/live.sections.txt
readelf -SW /repro/$repro_file > /report/reproduced.sections.txt
awk '\$2 == \".gnu.linkonce.this_module\" {print \$6}' /report/live.sections.txt > /report/live.this-module-size.txt
awk '\$2 == \".gnu.linkonce.this_module\" {print \$6}' /report/reproduced.sections.txt > /report/reproduced.this-module-size.txt
diff -u /report/live.undefined.txt /report/reproduced.undefined.txt > /report/undefined.diff || true
diff -u /report/live.modinfo.txt /report/reproduced.modinfo.txt > /report/modinfo.diff || true
diff -u /report/live.this-module-size.txt /report/reproduced.this-module-size.txt > /report/this-module-size.diff || true
"

if cmp -s "$live_module" "$reproduced_module"; then
    echo "byte-identical: yes"
else
    echo "byte-identical: no"
fi

if [[ -s "$report_dir/undefined.diff" ]]; then
    echo "undefined-symbol set differs; inspect $report_dir/undefined.diff" >&2
    exit 1
fi

echo "undefined-symbol set: identical"

if [[ -s "$report_dir/modinfo.diff" ]]; then
    echo "module metadata differs; inspect $report_dir/modinfo.diff" >&2
    exit 1
fi

echo "module metadata: identical"

if [[ -s "$report_dir/this-module-size.diff" ]]; then
    echo "kernel module ABI differs; inspect $report_dir/this-module-size.diff" >&2
    exit 1
fi

echo ".gnu.linkonce.this_module size: identical"
echo "report: $report_dir"
