#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-ax3000t-stage4-builder:ubuntu22.04-pinned}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out}"
JOBS="${JOBS:-2}"
WORK_VOLUME="${WORK_VOLUME:-ax3000t-stage4-work-$(date -u +%Y%m%dt%H%M%Sz)-$$}"
WORK_VOLUME_SESSION="${WORK_VOLUME_SESSION:-manual-$(date -u +%Y%m%dt%H%M%Sz)-$$}"
SIGNING_KEY_FILE="${STAGE4_SIGNING_KEY_FILE:-}"

case "$JOBS" in
  ''|*[!0-9]*) echo "JOBS must be an integer from 1 through 6" >&2; exit 2 ;;
esac
if (( JOBS < 1 || JOBS > 6 )); then
  echo "JOBS must be between 1 and 6; default is the memory-safe value 2" >&2
  exit 2
fi
if [[ -z "$SIGNING_KEY_FILE" || ! -f "$SIGNING_KEY_FILE" || -L "$SIGNING_KEY_FILE" ]]; then
  echo "STAGE4_SIGNING_KEY_FILE must name the external dedicated private key" >&2
  exit 2
fi
if [[ "$(uname -s)" == Darwin ]]; then
  SIGNING_MODE="$(stat -f '%Lp' "$SIGNING_KEY_FILE")"
else
  SIGNING_MODE="$(stat -c '%a' "$SIGNING_KEY_FILE")"
fi
if [[ "$SIGNING_MODE" != 600 ]]; then
  echo "external Stage4 signing key must have mode 0600" >&2
  exit 2
fi
SIGNING_KEY_FILE="$(python3 - "$SIGNING_KEY_FILE" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).resolve(strict=True))
PY
)"

if ! SOURCE_REPO="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Stage4 source must be integrated into a Git repository before an exact build" >&2
  exit 2
fi
SOURCE_REPO="$(cd "$SOURCE_REPO" && pwd)"
case "$ROOT_DIR/" in
  "$SOURCE_REPO/"*) ;;
  *) echo "Stage4 directory is outside the resolved Git repository" >&2; exit 2 ;;
esac
ROOT_REL="${ROOT_DIR#"$SOURCE_REPO"/}"
[[ "$ROOT_REL" != "$ROOT_DIR" ]] || ROOT_REL="."
if [[ -n "$(git -C "$SOURCE_REPO" status --porcelain --untracked-files=all -- "$ROOT_REL")" ]]; then
  echo "Stage4 source is modified or untracked; commit it before the exact build" >&2
  exit 2
fi
STAGE4_SOURCE_COMMIT="$(git -C "$SOURCE_REPO" rev-parse HEAD)"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
SIGNING_PARENT="$(cd "$(dirname "$SIGNING_KEY_FILE")" && pwd)"
if [[ "$(uname -s)" == Darwin ]]; then
  SIGNING_NLINK="$(stat -f '%l' "$SIGNING_KEY_FILE")"
  SIGNING_PARENT_MODE="$(stat -f '%Lp' "$SIGNING_PARENT")"
else
  SIGNING_NLINK="$(stat -c '%h' "$SIGNING_KEY_FILE")"
  SIGNING_PARENT_MODE="$(stat -c '%a' "$SIGNING_PARENT")"
fi
path_within() {
  [[ "$1" == "$2" || "$1" == "$2/"* ]]
}
if [[ "$SIGNING_NLINK" != 1 || "$SIGNING_PARENT_MODE" != 700 ]] ||
   path_within "$SIGNING_KEY_FILE" "$SOURCE_REPO" ||
   path_within "$SIGNING_KEY_FILE" "$ROOT_DIR" ||
   path_within "$SIGNING_KEY_FILE" "$OUT_DIR"; then
  echo "private signing key must have one link, a mode-0700 parent, and live outside source/output trees" >&2
  exit 2
fi
AVAILABLE_KIB="$(df -Pk "$OUT_DIR" | awk 'NR == 2 { print $4 }')"
if [[ ! "$AVAILABLE_KIB" =~ ^[0-9]+$ ]] || (( AVAILABLE_KIB < 25 * 1024 * 1024 )); then
  echo "refusing to start: output filesystem has less than 25 GiB available" >&2
  exit 2
fi
if docker volume inspect "$WORK_VOLUME" >/dev/null 2>&1; then
  echo "refusing to reuse Docker work volume: $WORK_VOLUME" >&2
  exit 2
fi
if [[ ! "$WORK_VOLUME" =~ ^ax3000t-stage4-[a-z0-9][a-z0-9._-]{0,95}$ ||
      ! "$WORK_VOLUME_SESSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$ ]]; then
  echo "work volume name/session is outside the dedicated Stage4 namespace" >&2
  exit 2
fi

SNAPSHOT_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/ax3000t-stage4-source.XXXXXX")"
cleanup_snapshot() {
  case "$SNAPSHOT_PARENT" in
    "${TMPDIR:-/tmp}"/ax3000t-stage4-source.*) rm -rf -- "$SNAPSHOT_PARENT" ;;
    *) echo "refusing unsafe source snapshot cleanup" >&2 ;;
  esac
}
trap cleanup_snapshot EXIT
mkdir "$SNAPSHOT_PARENT/tree"
SOURCE_ARCHIVE="$SNAPSHOT_PARENT/source.tar"
if [[ "$ROOT_REL" == . ]]; then
  git -C "$SOURCE_REPO" archive --format=tar "$STAGE4_SOURCE_COMMIT" > "$SOURCE_ARCHIVE"
  SNAPSHOT_ROOT="$SNAPSHOT_PARENT/tree"
  STAGE4_SOURCE_TREE="$(git -C "$SOURCE_REPO" rev-parse "$STAGE4_SOURCE_COMMIT^{tree}")"
else
  git -C "$SOURCE_REPO" archive --format=tar "$STAGE4_SOURCE_COMMIT" -- "$ROOT_REL" > "$SOURCE_ARCHIVE"
  SNAPSHOT_ROOT="$SNAPSHOT_PARENT/tree/$ROOT_REL"
  STAGE4_SOURCE_TREE="$(git -C "$SOURCE_REPO" rev-parse "$STAGE4_SOURCE_COMMIT:$ROOT_REL")"
fi
if git -C "$SOURCE_REPO" ls-tree -r "$STAGE4_SOURCE_COMMIT" -- "$ROOT_REL" | \
     grep -Eq '^(120000|160000) '; then
  echo "Stage4 committed source contains a symlink or submodule/gitlink" >&2
  exit 2
fi
if [[ "$(uname -s)" == Darwin ]]; then
  STAGE4_SOURCE_ARCHIVE_SHA256="$(shasum -a 256 "$SOURCE_ARCHIVE" | awk '{print $1}')"
else
  STAGE4_SOURCE_ARCHIVE_SHA256="$(sha256sum "$SOURCE_ARCHIVE" | awk '{print $1}')"
fi
tar -xf "$SOURCE_ARCHIVE" -C "$SNAPSHOT_PARENT/tree"
if [[ ! -d "$SNAPSHOT_ROOT" || -L "$SNAPSHOT_ROOT" ||
      ! -f "$SNAPSHOT_ROOT/source-lock.json" || -L "$SNAPSHOT_ROOT/source-lock.json" ]]; then
  echo "canonical committed Stage4 source snapshot is incomplete or unsafe" >&2
  exit 2
fi

BUILDER_IID_FILE="$SNAPSHOT_PARENT/builder-image-id"
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
  --build-arg SOURCE_DATE_EPOCH=1782770622 \
  --iidfile "$BUILDER_IID_FILE" \
  -t "$IMAGE_TAG" "$SNAPSHOT_ROOT/container"
BUILDER_IMAGE_ID="$(tr -d '\r\n' < "$BUILDER_IID_FILE")"
if [[ ! "$BUILDER_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] ||
   [[ "$(docker image inspect --format '{{.Id}}' "$BUILDER_IMAGE_ID")" != "$BUILDER_IMAGE_ID" ]]; then
  echo "Docker did not return an immutable builder image ID" >&2
  exit 2
fi
docker volume create \
  --label com.howtion.ax3000t-stage4.managed=true \
  --label "com.howtion.ax3000t-stage4.session=$WORK_VOLUME_SESSION" \
  "$WORK_VOLUME" >/dev/null
VOLUME_MANAGED="$(docker volume inspect \
  --format '{{ index .Labels "com.howtion.ax3000t-stage4.managed" }}' \
  "$WORK_VOLUME")"
VOLUME_SESSION="$(docker volume inspect \
  --format '{{ index .Labels "com.howtion.ax3000t-stage4.session" }}' \
  "$WORK_VOLUME")"
if [[ "$VOLUME_MANAGED" != true || "$VOLUME_SESSION" != "$WORK_VOLUME_SESSION" ]]; then
  echo "created Docker volume failed the exact Stage4 ownership-label check" >&2
  exit 2
fi
if ! docker run --rm --platform linux/amd64 \
  -v "$WORK_VOLUME:/work:ro" \
  "$BUILDER_IMAGE_ID" \
  -lc 'test -z "$(find /work -mindepth 1 -maxdepth 1 -print -quit)"'; then
  echo "created Docker volume is not empty; refusing to reuse it" >&2
  exit 2
fi
docker run --rm --platform linux/amd64 \
  -v "$WORK_VOLUME:/work" \
  "$BUILDER_IMAGE_ID" \
  -lc "chown $(id -u):$(id -g) /work"

echo "Case-sensitive build volume: $WORK_VOLUME"
echo "A failed build retains this volume for diagnostics. The repro-pair wrapper removes only"
echo "its own exactly labelled volume after the exported output has passed verification."
# Every run has its own fresh named volume, so the inner path can and must stay
# fixed.  A timestamp/PID here would leak into compiler paths and defeat the
# two-clean-build byte-identity gate even when every source byte is identical.
BUILD_SUBDIR="build"
echo "Phase 1/2: networked fetch and hash-verified download only."
docker run --rm --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp/ax3000t-builder \
  -e JOBS="$JOBS" \
  -e BUILDER_IMAGE_ID="$BUILDER_IMAGE_ID" \
  -e BUILDER_BASE_DIGEST="ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982" \
  -e STAGE4_SOURCE_COMMIT="$STAGE4_SOURCE_COMMIT" \
  -e STAGE4_SOURCE_TREE="$STAGE4_SOURCE_TREE" \
  -e STAGE4_SOURCE_ARCHIVE_SHA256="$STAGE4_SOURCE_ARCHIVE_SHA256" \
  -e STAGE4_PHASE=prepare \
  -v "$SNAPSHOT_ROOT:/stage4:ro" \
  -v "$WORK_VOLUME:/work" \
  -v "$OUT_DIR:/out" \
  "$BUILDER_IMAGE_ID" \
  -lc "mkdir -p \"\$HOME\"; /stage4/scripts/build_image.sh /work/$BUILD_SUBDIR /out"

echo "Phase 2/2: build and verification with Docker networking disabled."
docker run --rm --platform linux/amd64 \
  --network=none \
  --user "$(id -u):$(id -g)" \
  --tmpfs "/run/stage4-signing:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid=$(id -u),gid=$(id -g)" \
  -e HOME=/tmp/ax3000t-builder \
  -e JOBS="$JOBS" \
  -e BUILDER_IMAGE_ID="$BUILDER_IMAGE_ID" \
  -e BUILDER_BASE_DIGEST="ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982" \
  -e STAGE4_SOURCE_COMMIT="$STAGE4_SOURCE_COMMIT" \
  -e STAGE4_SOURCE_TREE="$STAGE4_SOURCE_TREE" \
  -e STAGE4_SOURCE_ARCHIVE_SHA256="$STAGE4_SOURCE_ARCHIVE_SHA256" \
  -e STAGE4_PHASE=build \
  -e STAGE4_SIGNING_KEY_FILE=/run/ax3000t-stage4-private \
  -e STAGE4_SIGNING_RUNTIME_DIR=/run/stage4-signing \
  --mount "type=bind,src=$SIGNING_KEY_FILE,dst=/run/ax3000t-stage4-private,readonly" \
  -v "$SNAPSHOT_ROOT:/stage4:ro" \
  -v "$WORK_VOLUME:/work" \
  -v "$OUT_DIR:/out" \
  "$BUILDER_IMAGE_ID" \
  -lc "mkdir -p \"\$HOME\"; /stage4/scripts/build_image.sh /work/$BUILD_SUBDIR /out"
