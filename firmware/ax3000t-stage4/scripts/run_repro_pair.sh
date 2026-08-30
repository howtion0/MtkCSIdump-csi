#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAIR_ROOT="${PAIR_ROOT:-$ROOT_DIR/out/repro-pair}"
FIRST_OUT="$PAIR_ROOT/first"
SECOND_OUT="$PAIR_ROOT/second"

if [[ -e "$PAIR_ROOT" ]]; then
  echo "PAIR_ROOT must be new; refusing to reuse $PAIR_ROOT" >&2
  exit 2
fi
mkdir -p "$FIRST_OUT" "$SECOND_OUT"

PAIR_SESSION="repro-$(date -u +%Y%m%dt%H%M%Sz)-$$"
FIRST_VOLUME="ax3000t-stage4-repro-a-${PAIR_SESSION}"
SECOND_VOLUME="ax3000t-stage4-repro-b-${PAIR_SESSION}"

verify_exported_output() {
  python3 - "$1" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
image_name = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"
required = (image_name, "gate-report.json", "build-provenance.json", "SHA256SUMS")
if any(not (root / name).is_file() or (root / name).is_symlink() for name in required):
    raise SystemExit("exported build output lacks required regular files")

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

gate = json.loads((root / "gate-report.json").read_text(encoding="utf-8"))
provenance = json.loads((root / "build-provenance.json").read_text(encoding="utf-8"))
image = root / image_name
if (gate.get("result") != "pass" or gate.get("classification") !=
        "EXPERIMENTAL-DO-NOT-FLASH" or gate.get("flash_authorized") is not False):
    raise SystemExit("exported final-image gate report is not a fail-closed PASS")
identity = {"name": image_name, "bytes": image.stat().st_size, "sha256": digest(image)}
if provenance.get("image") != identity or provenance.get("gate_result") != "pass":
    raise SystemExit("exported provenance is not cross-bound to the passing image")
lines = (root / "SHA256SUMS").read_text(encoding="ascii").splitlines()
expected_names = {image_name, "gate-report.json", "build-provenance.json"}
actual = {}
for line in lines:
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
    if not match or match.group(2) in actual:
        raise SystemExit("SHA256SUMS is malformed or duplicated")
    actual[match.group(2)] = match.group(1)
if set(actual) != expected_names or any(
        digest(root / name) != value for name, value in actual.items()):
    raise SystemExit("SHA256SUMS does not close over the exported build output")
PY
}

remove_owned_volume() {
  local volume="$1"
  local session="$2"
  local managed actual_session
  managed="$(docker volume inspect --format '{{ index .Labels "com.howtion.ax3000t-stage4.managed" }}' "$volume")"
  actual_session="$(docker volume inspect --format '{{ index .Labels "com.howtion.ax3000t-stage4.session" }}' "$volume")"
  if [[ "$volume" != ax3000t-stage4-repro-* || "$managed" != true ||
        "$actual_session" != "$session" ]]; then
    echo "refusing to remove a volume outside this exact Stage4 repro session" >&2
    return 1
  fi
  docker volume rm "$volume" >/dev/null
}

echo "Clean build 1/2"
OUT_DIR="$FIRST_OUT" WORK_VOLUME="$FIRST_VOLUME" WORK_VOLUME_SESSION="$PAIR_SESSION" \
  "$ROOT_DIR/scripts/run_container_build.sh"
verify_exported_output "$FIRST_OUT"
remove_owned_volume "$FIRST_VOLUME" "$PAIR_SESSION"

echo "Clean build 2/2"
OUT_DIR="$SECOND_OUT" WORK_VOLUME="$SECOND_VOLUME" WORK_VOLUME_SESSION="$PAIR_SESSION" \
  "$ROOT_DIR/scripts/run_container_build.sh"
verify_exported_output "$SECOND_OUT"

python3 "$ROOT_DIR/scripts/compare_repro_builds.py" \
  --first "$FIRST_OUT" --second "$SECOND_OUT"
remove_owned_volume "$SECOND_VOLUME" "$PAIR_SESSION"
echo "Canonical audited output: $FIRST_OUT"
echo "Both fresh work volumes were removed only after their exported outputs passed all gates."
