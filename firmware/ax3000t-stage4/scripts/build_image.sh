#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 NEW_WORK_DIRECTORY NEW_OR_EMPTY_OUTPUT_DIRECTORY" >&2
  exit 2
fi

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$1"
OUT_DIR="$2"
OPENWRT_COMMIT="f0a60eee2fe051741c643ea6118718aae1ef17fb"
OPENWRT_REVISION="r33051-f5dae5ece4"
KWRT_COMMIT="aae059682faae01d600db7061c150f65de87a21e"
SOURCE_DATE_EPOCH="1782770622"
JOBS="${JOBS:-2}"
STAGE4_SOURCE_COMMIT="${STAGE4_SOURCE_COMMIT:-}"
STAGE4_SOURCE_TREE="${STAGE4_SOURCE_TREE:-}"
STAGE4_SOURCE_ARCHIVE_SHA256="${STAGE4_SOURCE_ARCHIVE_SHA256:-}"
BUILDER_IMAGE_ID="${BUILDER_IMAGE_ID:-unknown}"
BUILDER_BASE_DIGEST="${BUILDER_BASE_DIGEST:-unknown}"
SIGNING_INPUT="${STAGE4_SIGNING_KEY_FILE:-}"
SIGNING_RUNTIME_DIR="${STAGE4_SIGNING_RUNTIME_DIR:-/run/stage4-signing}"
PHASE="${STAGE4_PHASE:-}"
export SOURCE_DATE_EPOCH
export TZ=UTC
export LANG=C
export LC_ALL=C

if [[ "$PHASE" != prepare && "$PHASE" != build ]]; then
  echo "STAGE4_PHASE must be prepare (networked fetch/download) or build (network disabled)" >&2
  exit 2
fi

case "$JOBS" in
  ''|*[!0-9]*) echo "JOBS must be an integer from 1 through 6" >&2; exit 2 ;;
esac
if (( JOBS < 1 || JOBS > 6 )); then
  echo "JOBS must be between 1 and 6; default is 2" >&2
  exit 2
fi
if [[ ! "$STAGE4_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STAGE4_SOURCE_COMMIT must identify the clean, committed Stage4 source" >&2
  exit 2
fi
if [[ ! "$STAGE4_SOURCE_TREE" =~ ^[0-9a-f]{40}$ ||
      ! "$STAGE4_SOURCE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "canonical committed Stage4 source tree/archive identity is missing" >&2
  exit 2
fi
if [[ "$BUILDER_BASE_DIGEST" != "ubuntu@sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982" ]]; then
  echo "builder base digest is missing or differs from the locked Ubuntu image" >&2
  exit 2
fi
if [[ ! "$BUILDER_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "BUILDER_IMAGE_ID must be the inspected local builder image ID" >&2
  exit 2
fi

case "$WORK_DIR" in
  /|""|"$HOME") echo "refusing unsafe work directory: $WORK_DIR" >&2; exit 2 ;;
esac
if [[ "$PHASE" == prepare && -e "$WORK_DIR" ]]; then
  echo "prepare phase requires a new work directory: $WORK_DIR" >&2
  exit 2
elif [[ "$PHASE" == build && ( ! -d "$WORK_DIR" || -L "$WORK_DIR" ) ]]; then
  echo "build phase requires the prepared regular work directory: $WORK_DIR" >&2
  exit 2
fi
if [[ -e "$OUT_DIR" ]] && [[ -n "$(find "$OUT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "refusing non-empty output directory: $OUT_DIR" >&2
  exit 2
fi

for tool in git patch make python3 sha256sum unsquashfs tar findmnt; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 2; }
done

PUBLIC_KEY="$STAGE_DIR/keys/ax3000t-stage4.pub"
BASE_UCERT="$STAGE_DIR/keys/ax3000t-stage4.ucert"
python3 - "$STAGE_DIR/source-lock.json" "$PUBLIC_KEY" "$BASE_UCERT" <<'PY'
import hashlib, json, re, sys
from pathlib import Path
lock = json.loads(Path(sys.argv[1]).read_text())["signing"]
pub, cert = Path(sys.argv[2]), Path(sys.argv[3])
if lock.get("status") != "READY":
    raise SystemExit("Stage4 signing lock is not READY")
for path, field in ((pub, "public_key_sha256"), (cert, "base_ucert_sha256")):
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"missing or unsafe public signing input: {path.name}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != lock.get(field):
        raise SystemExit(f"public signing input hash mismatch: {path.name}")
if not re.fullmatch(r"[0-9a-f]{16,64}", str(lock.get("usign_fingerprint") or "")):
    raise SystemExit("locked usign fingerprint is absent or malformed")
PY
RUNTIME_KEY=
if [[ "$PHASE" == build ]]; then
  if [[ -z "$SIGNING_INPUT" || ! -f "$SIGNING_INPUT" || -L "$SIGNING_INPUT" ]]; then
    echo "STAGE4_SIGNING_KEY_FILE must name the dedicated external regular private key" >&2
    exit 2
  fi
  SIGNING_MODE="$(stat -c '%a' "$SIGNING_INPUT")"
  if [[ "$SIGNING_MODE" != 600 ]]; then
    echo "external Stage4 signing key must have mode 0600" >&2
    exit 2
  fi
  if [[ "$(findmnt -n -o FSTYPE -T "$SIGNING_RUNTIME_DIR" 2>/dev/null || true)" != tmpfs ]]; then
    echo "STAGE4_SIGNING_RUNTIME_DIR must be a dedicated tmpfs" >&2
    exit 2
  fi
  RUNTIME_KEY="$SIGNING_RUNTIME_DIR/key-build"
  install -m 0600 "$SIGNING_INPUT" "$RUNTIME_KEY"
  install -m 0644 "$PUBLIC_KEY" "$RUNTIME_KEY.pub"
  install -m 0644 "$BASE_UCERT" "$RUNTIME_KEY.ucert"
  cleanup_signing_key() {
    rm -f "$RUNTIME_KEY" "$RUNTIME_KEY.pub" "$RUNTIME_KEY.ucert"
  }
  trap cleanup_signing_key EXIT
fi

mkdir -p "$WORK_DIR" "$OUT_DIR"
OPENWRT="$WORK_DIR/openwrt"
KWRT="$WORK_DIR/kwrt-lock"
LOG="$WORK_DIR/build.log"
MAKE=(make -C "$OPENWRT" REVISION="$OPENWRT_REVISION" SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH")
if [[ "$PHASE" == build ]]; then
  MAKE+=(BUILD_KEY="$RUNTIME_KEY")
fi

require_disk_space() {
  local available_kib
  available_kib="$(df -Pk "$WORK_DIR" | awk 'NR == 2 { print $4 }')"
  if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || (( available_kib < 25 * 1024 * 1024 )); then
    echo "refusing to continue: build filesystem has less than 25 GiB available" | tee -a "$LOG" >&2
    exit 1
  fi
}

if [[ "$PHASE" == prepare ]]; then
echo "[1/11] Fetching exact public revisions" | tee "$LOG"
require_disk_space
dpkg-query -W -f='${Package}=${Version}\n' | LC_ALL=C sort > "$WORK_DIR/builder-packages.txt"
git init -q "$OPENWRT"
git -C "$OPENWRT" remote add origin https://git.openwrt.org/openwrt/openwrt.git
git -C "$OPENWRT" fetch --depth=1 --filter=blob:none origin "$OPENWRT_COMMIT" 2>&1 | tee -a "$LOG"
git -C "$OPENWRT" checkout -q --detach FETCH_HEAD

git init -q "$KWRT"
git -C "$KWRT" remote add origin https://github.com/kiddin9/Kwrt.git
git -C "$KWRT" fetch --depth=1 --filter=blob:none origin "$KWRT_COMMIT" 2>&1 | tee -a "$LOG"
git -C "$KWRT" checkout -q --detach FETCH_HEAD

echo "[2/11] Verifying pristine source and exact historical patch origin" | tee -a "$LOG"
python3 "$STAGE_DIR/scripts/verify_sources.py" \
  --lock "$STAGE_DIR/source-lock.json" \
  --patch-dir "$STAGE_DIR/patches" \
  --openwrt "$OPENWRT" \
  --kwrt "$KWRT" \
  --phase pristine \
  --output "$WORK_DIR/source-pristine-gates.json" | tee -a "$LOG"

echo "[3/11] Applying locked Kwrt ABI, 112 MiB layout, and anti-misflash metadata patches" | tee -a "$LOG"
for patch_name in 10-kwrt-vermagic-one.patch 23-ax3000t.patch 25-platform.patch 26-ax3000t-single-ubi-compat.patch; do
  if [[ "$patch_name" == "25-platform.patch" ]]; then
    # The exact historical 4a9815... file is missing only its final LF.
    # Preserve those locked bytes; normalize the patch input stream, whose
    # independent SHA-256 (9be8ed...) is also locked and verified.
    { cat "$STAGE_DIR/patches/$patch_name"; printf '\n'; } | \
      patch -d "$OPENWRT" --dry-run --no-backup-if-mismatch -p1 -F 0 -i - >/dev/null
    { cat "$STAGE_DIR/patches/$patch_name"; printf '\n'; } | \
      patch -d "$OPENWRT" --no-backup-if-mismatch -p1 -F 0 -i - | tee -a "$LOG"
  else
    patch -d "$OPENWRT" --dry-run --no-backup-if-mismatch -p1 -F 0 \
      -i "$STAGE_DIR/patches/$patch_name" >/dev/null
    patch -d "$OPENWRT" --no-backup-if-mismatch -p1 -F 0 \
      -i "$STAGE_DIR/patches/$patch_name" | tee -a "$LOG"
  fi
done

echo "[4/11] Deriving the build seed from the exact historical Kwrt configuration" | tee -a "$LOG"
cp -R "$STAGE_DIR/package/mtkcsi-dump" "$OPENWRT/package/utils/mtkcsi-dump"
{
  cat "$KWRT/devices/common/.config"
  printf '\n'
  cat "$KWRT/devices/mediatek_filogic/.config"
} > "$WORK_DIR/kwrt-exact.config"
sed -E \
  -e '/^CONFIG_TARGET_(MULTI_PROFILE|ALL_PROFILES)=/d' \
  -e '/^CONFIG_TARGET_[^=]+_DEVICE_.*=/d' \
  -e '/^CONFIG_PACKAGE_mtkcsi-dump=/d' \
  -e '/^CONFIG_KERNEL_BUILD_USER=/d' \
  -e '/^# CONFIG_KERNEL_BUILD_USER is not set$/d' \
  -e '/^CONFIG_KERNEL_BUILD_DOMAIN=/d' \
  -e '/^# CONFIG_KERNEL_BUILD_DOMAIN is not set$/d' \
  -e '/^CONFIG_SIGNED_PACKAGES=/d' \
  -e '/^CONFIG_SIGNATURE_CHECK=/d' \
  -e '/^CONFIG_PER_FEED_REPO=/d' \
  -e '/^# CONFIG_PER_FEED_REPO is not set$/d' \
  -e '/^CONFIG_VERSION_DIST=/d' \
  -e '/^CONFIG_VERSION_NUMBER=/d' \
  -e '/^CONFIG_VERSION_CODE=/d' \
  -e '/^CONFIG_VERSION_REPO=/d' \
  -e '/^CONFIG_VERSION_MANUFACTURER=/d' \
  -e '/^CONFIG_VERSION_HOME_URL=/d' \
  -e '/^CONFIG_VERSION_MANUFACTURER_URL=/d' \
  "$WORK_DIR/kwrt-exact.config" > "$OPENWRT/.config"
printf '\n' >> "$OPENWRT/.config"
cat "$STAGE_DIR/build-config.seed" >> "$OPENWRT/.config"
"${MAKE[@]}" defconfig 2>&1 | tee -a "$LOG"
grep -qx 'CONFIG_TARGET_mediatek_filogic_DEVICE_xiaomi_mi-router-ax3000t=y' "$OPENWRT/.config"
grep -qx 'CONFIG_PACKAGE_kmod-mt7915e=y' "$OPENWRT/.config"
grep -qx 'CONFIG_PACKAGE_mtkcsi-dump=y' "$OPENWRT/.config"
grep -qx 'CONFIG_KERNEL_BUILD_USER="builder"' "$OPENWRT/.config"
grep -qx 'CONFIG_KERNEL_BUILD_DOMAIN="buildhost"' "$OPENWRT/.config"
grep -qx '# CONFIG_USE_APK is not set' "$OPENWRT/.config"
! grep -q '^CONFIG_USE_APK=y$' "$OPENWRT/.config"
grep -qx 'CONFIG_SIGNED_PACKAGES=y' "$OPENWRT/.config"
grep -qx 'CONFIG_SIGNATURE_CHECK=y' "$OPENWRT/.config"
grep -qx '# CONFIG_PER_FEED_REPO is not set' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_DIST="OpenWrt-CSI-Lab"' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_NUMBER="25.12.5-experimental"' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_CODE="ax3000t-single-ubi-112m-csi"' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_REPO="file:///nonexistent/ax3000t-112m-csi-packages"' "$OPENWRT/.config"
! grep -q 'dl\.openwrt\.ai' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_MANUFACTURER="OpenWrt CSI Lab"' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_HOME_URL="https://github.com/howtion0/MtkCSIdump-csi"' "$OPENWRT/.config"
grep -qx 'CONFIG_VERSION_MANUFACTURER_URL="https://github.com/howtion0/MtkCSIdump-csi"' "$OPENWRT/.config"
! grep -Eqi 'openwrt\.ai|Kiddin' "$OPENWRT/.config"
EXTRA_TARGETS="$(grep -E '^CONFIG_TARGET_[^=]+_DEVICE_.*=y' "$OPENWRT/.config" | \
  grep -v '^CONFIG_TARGET_mediatek_filogic_DEVICE_xiaomi_mi-router-ax3000t=y$' || true)"
if [[ -n "$EXTRA_TARGETS" ]]; then
  echo "unexpected extra target device" >&2
  printf '%s\n' "$EXTRA_TARGETS" >&2
  exit 1
fi
cp "$OPENWRT/.config" "$WORK_DIR/final.config"

echo "[5/11] Downloading hash-verified sources" | tee -a "$LOG"
require_disk_space
"${MAKE[@]}" -j"$JOBS" download 2>&1 | tee -a "$LOG"
if find "$OPENWRT/dl" -type f -size -1024c -print -quit | grep -q .; then
  echo "a suspiciously short download remains in dl/" >&2
  exit 1
fi
CAPTURE_ARCHIVE="$OPENWRT/dl/mtkcsi-dump-2.0.0~git20260830.b8d7b73.tar.zst"
python3 "$STAGE_DIR/scripts/verify_capture_archive.py" \
  --archive "$CAPTURE_ARCHIVE" \
  --package-makefile "$STAGE_DIR/package/mtkcsi-dump/Makefile" \
  --source-lock "$STAGE_DIR/source-lock.json" \
  --output "$WORK_DIR/capture-source-gates.json" | tee -a "$LOG"

python3 "$STAGE_DIR/scripts/download_closure.py" create \
  --root "$OPENWRT/dl" --manifest "$WORK_DIR/download-closure.json" | tee -a "$LOG"
python3 - "$WORK_DIR/.stage4-prepared.json" "$STAGE4_SOURCE_COMMIT" \
  "$STAGE4_SOURCE_TREE" "$STAGE4_SOURCE_ARCHIVE_SHA256" \
  "$BUILDER_IMAGE_ID" "$BUILDER_BASE_DIGEST" "$OPENWRT_COMMIT" "$KWRT_COMMIT" \
  "$WORK_DIR/final.config" "$WORK_DIR/download-closure.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
marker = {
    "schema": 1,
    "phase": "networked-prepare-complete",
    "stage4_source_commit": sys.argv[2],
    "stage4_source_tree": sys.argv[3],
    "stage4_source_archive_sha256": sys.argv[4],
    "builder_image_id": sys.argv[5],
    "builder_base_digest": sys.argv[6],
    "openwrt_commit": sys.argv[7],
    "kwrt_commit": sys.argv[8],
    "final_config_sha256": digest(sys.argv[9]),
    "download_manifest_sha256": digest(sys.argv[10]),
}
Path(sys.argv[1]).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
PY
echo "Networked prepare/download phase complete. Re-run only the build phase with --network=none."
exit 0
fi

PREPARED_MARKER="$WORK_DIR/.stage4-prepared.json"
if [[ ! -f "$PREPARED_MARKER" || -L "$PREPARED_MARKER" || \
      ! -f "$WORK_DIR/download-closure.json" || -L "$WORK_DIR/download-closure.json" ]]; then
  echo "prepared phase marker/download manifest is missing or unsafe" >&2
  exit 2
fi
python3 - "$PREPARED_MARKER" "$STAGE4_SOURCE_COMMIT" \
  "$STAGE4_SOURCE_TREE" "$STAGE4_SOURCE_ARCHIVE_SHA256" "$BUILDER_IMAGE_ID" \
  "$BUILDER_BASE_DIGEST" "$OPENWRT_COMMIT" "$KWRT_COMMIT" \
  "$WORK_DIR/final.config" "$WORK_DIR/download-closure.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
marker = json.loads(Path(sys.argv[1]).read_text())
expected = {
    "schema": 1,
    "phase": "networked-prepare-complete",
    "stage4_source_commit": sys.argv[2],
    "stage4_source_tree": sys.argv[3],
    "stage4_source_archive_sha256": sys.argv[4],
    "builder_image_id": sys.argv[5],
    "builder_base_digest": sys.argv[6],
    "openwrt_commit": sys.argv[7],
    "kwrt_commit": sys.argv[8],
    "final_config_sha256": digest(sys.argv[9]),
    "download_manifest_sha256": digest(sys.argv[10]),
}
if marker != expected:
    raise SystemExit("prepared phase marker does not match this clean source/builder/config/download set")
PY
python3 "$STAGE_DIR/scripts/download_closure.py" verify \
  --root "$OPENWRT/dl" --manifest "$WORK_DIR/download-closure.json" | tee -a "$LOG"
printf '%s\n' '[offline-build] prepared source/download closure verified' | tee -a "$LOG"

echo "[6/11] Building and rejecting/accepting the vanilla Kwrt ABI control" | tee -a "$LOG"
require_disk_space
"${MAKE[@]}" -j"$JOBS" package/system/usign/host/compile package/system/ucert/host/compile \
  2>&1 | tee -a "$LOG"
USIGN_HOST="$OPENWRT/staging_dir/host/bin/usign"
UCERT_HOST="$OPENWRT/staging_dir/host/bin/ucert"
[[ -x "$USIGN_HOST" && -x "$UCERT_HOST" ]] || { echo "host usign/ucert tools are missing" >&2; exit 1; }
LOCKED_FINGERPRINT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["signing"]["usign_fingerprint"])' "$STAGE_DIR/source-lock.json")"
PUBLIC_FINGERPRINT="$($USIGN_HOST -F -p "$RUNTIME_KEY.pub")"
PRIVATE_FINGERPRINT="$($USIGN_HOST -F -s "$RUNTIME_KEY")"
if [[ "$PUBLIC_FINGERPRINT" != "$LOCKED_FINGERPRINT" || "$PRIVATE_FINGERPRINT" != "$LOCKED_FINGERPRINT" ]]; then
  echo "external private key, pinned public key, and locked fingerprint do not match" >&2
  exit 1
fi
"${MAKE[@]}" -j"$JOBS" package/kernel/mt76/compile 2>&1 | tee -a "$LOG"
mapfile -t VANILLA_MODULES < <(find "$OPENWRT/build_dir" -type f \
  -path '*/ipkg-*/kmod-mt7915e/lib/modules/*/mt7915e.ko' | sort)
mapfile -t VANILLA_IPKS < <(find "$OPENWRT/bin" -type f \
  -name 'kmod-mt7915e_6.12.94.2026.03.19~39c960c3-r2_aarch64_cortex-a53.ipk' | sort)
[[ ${#VANILLA_MODULES[@]} -eq 1 ]] || { printf 'expected one vanilla mt7915e.ko, found %s\n' "${#VANILLA_MODULES[@]}" >&2; exit 1; }
[[ ${#VANILLA_IPKS[@]} -eq 1 ]] || { echo "unique vanilla Kwrt IPK not found" >&2; exit 1; }
install -m 0644 "${VANILLA_MODULES[0]}" "$WORK_DIR/mt7915e.vanilla.ko"
install -m 0644 "${VANILLA_IPKS[0]}" "$WORK_DIR/kmod-mt7915e.vanilla.ipk"
python3 "$STAGE_DIR/scripts/verify_vanilla_abi.py" \
  --module "$WORK_DIR/mt7915e.vanilla.ko" \
  --ipk "$WORK_DIR/kmod-mt7915e.vanilla.ipk" \
  --output "$WORK_DIR/vanilla-abi-gates.json" | tee -a "$LOG"

echo "[7/11] Adding the hardened CSI patch only after the vanilla ABI gate" | tee -a "$LOG"
mkdir -p "$OPENWRT/package/kernel/mt76/patches"
cp "$STAGE_DIR/patches/999-mt7915-csi-v2-hardened.patch" \
  "$OPENWRT/package/kernel/mt76/patches/999-mt7915-csi-v2-hardened.patch"
python3 "$STAGE_DIR/scripts/verify_sources.py" \
  --lock "$STAGE_DIR/source-lock.json" \
  --patch-dir "$STAGE_DIR/patches" \
  --openwrt "$OPENWRT" \
  --kwrt "$KWRT" \
  --phase patched \
  --output "$WORK_DIR/source-patched-gates.json" | tee -a "$LOG"
"${MAKE[@]}" package/kernel/mt76/clean 2>&1 | tee -a "$LOG"

echo "[8/11] Building the generic experimental image" | tee -a "$LOG"
require_disk_space
if ! "${MAKE[@]}" -j"$JOBS" 2>&1 | tee -a "$LOG"; then
  echo "parallel build failed; preserving work tree and retrying serially for diagnostics" | tee -a "$LOG"
  "${MAKE[@]}" -j1 V=s 2>&1 | tee -a "$LOG"
fi

TARGET_OUT="$OPENWRT/bin/targets/mediatek/filogic"
mapfile -t IMAGES < <(find "$TARGET_OUT" -maxdepth 1 -type f \
  -name '*xiaomi_mi-router-ax3000t-squashfs-sysupgrade.bin' ! -name '*ubootmod*' | sort)
mapfile -t MANIFESTS < <(find "$TARGET_OUT" -maxdepth 1 -type f \
  -name '*xiaomi_mi-router-ax3000t.manifest' ! -name '*ubootmod*' | sort)
mapfile -t MODULES < <(find "$OPENWRT/build_dir" -type f \
  -path '*/ipkg-*/kmod-mt7915e/lib/modules/*/mt7915e.ko' | sort)
mapfile -t KMOD_IPKS < <(find "$OPENWRT/bin" -type f \
  -name 'kmod-mt7915e_6.12.94.2026.03.19~39c960c3-r2_aarch64_cortex-a53.ipk' | sort)
mapfile -t RELEASE_FILES < <(find "$OPENWRT/build_dir" -type f \
  -path '*/linux-mediatek_filogic/linux-6.12.94/include/config/kernel.release' | sort)
mapfile -t KERNEL_CONFIGS < <(find "$OPENWRT/build_dir" -type f \
  -path '*/linux-mediatek_filogic/linux-6.12.94/.config' | sort)
mapfile -t MODULE_SYMVERS < <(find "$OPENWRT/build_dir" -type f \
  -path '*/linux-mediatek_filogic/linux-6.12.94/Module.symvers' | sort)
mapfile -t PLATFORM_FILES < <(find "$OPENWRT/build_dir" -type f \
  -path '*/root-mediatek/lib/upgrade/platform.sh' | sort)

[[ ${#IMAGES[@]} -eq 1 ]] || { printf 'expected one image, found %s\n' "${#IMAGES[@]}" >&2; exit 1; }
[[ ${#MANIFESTS[@]} -eq 1 ]] || { printf 'expected one package manifest, found %s\n' "${#MANIFESTS[@]}" >&2; exit 1; }
[[ ${#MODULES[@]} -eq 1 ]] || { printf 'expected one built mt7915e.ko, found %s\n' "${#MODULES[@]}" >&2; exit 1; }
[[ ${#KMOD_IPKS[@]} -eq 1 ]] || { echo "unique patched Kwrt IPK not found" >&2; exit 1; }
[[ ${#RELEASE_FILES[@]} -eq 1 ]] || { echo "unique kernel.release not found" >&2; exit 1; }
[[ ${#KERNEL_CONFIGS[@]} -eq 1 ]] || { echo "unique final kernel .config not found" >&2; exit 1; }
[[ ${#MODULE_SYMVERS[@]} -eq 1 ]] || { echo "unique final kernel Module.symvers not found" >&2; exit 1; }
[[ ${#PLATFORM_FILES[@]} -eq 1 ]] || { printf 'expected one staged platform.sh, found %s\n' "${#PLATFORM_FILES[@]}" >&2; exit 1; }

echo "[9/11] Collecting public build outputs" | tee -a "$LOG"
require_disk_space
IMAGE_OUT="$OUT_DIR/ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"
install -m 0644 "${IMAGES[0]}" "$IMAGE_OUT"
install -m 0644 "${MANIFESTS[0]}" "$OUT_DIR/packages.manifest"
install -m 0644 "${MODULES[0]}" "$OUT_DIR/mt7915e.ko"
install -m 0644 "${KMOD_IPKS[0]}" "$OUT_DIR/kmod-mt7915e.ipk"
install -m 0644 "$WORK_DIR/mt7915e.vanilla.ko" "$OUT_DIR/mt7915e.vanilla.ko"
install -m 0644 "$WORK_DIR/kmod-mt7915e.vanilla.ipk" "$OUT_DIR/kmod-mt7915e.vanilla.ipk"
install -m 0644 "${RELEASE_FILES[0]}" "$OUT_DIR/kernel.release"
install -m 0644 "${KERNEL_CONFIGS[0]}" "$OUT_DIR/kernel.config"
install -m 0644 "${MODULE_SYMVERS[0]}" "$OUT_DIR/Module.symvers"
install -m 0644 "${PLATFORM_FILES[0]}" "$OUT_DIR/platform.sh"
install -m 0644 "$WORK_DIR/final.config" "$OUT_DIR/build.config"
install -m 0644 "$WORK_DIR/kwrt-exact.config" "$OUT_DIR/kwrt-exact.config"
install -m 0644 "$WORK_DIR/source-pristine-gates.json" "$OUT_DIR/source-pristine-gates.json"
install -m 0644 "$WORK_DIR/source-patched-gates.json" "$OUT_DIR/source-patched-gates.json"
install -m 0644 "$WORK_DIR/vanilla-abi-gates.json" "$OUT_DIR/vanilla-abi-gates.json"
install -m 0644 "$WORK_DIR/capture-source-gates.json" "$OUT_DIR/capture-source-gates.json"
install -m 0644 "$STAGE_DIR/source-lock.json" "$OUT_DIR/source-lock.json"
install -m 0644 "$WORK_DIR/builder-packages.txt" "$OUT_DIR/builder-packages.txt"
install -m 0644 "$WORK_DIR/.stage4-prepared.json" "$OUT_DIR/network-prepare-receipt.json"
install -m 0644 "$WORK_DIR/download-closure.json" "$OUT_DIR/download-closure.json"
install -m 0644 "$PUBLIC_KEY" "$OUT_DIR/ax3000t-stage4.pub"
install -m 0644 "$BASE_UCERT" "$OUT_DIR/ax3000t-stage4.ucert"

echo "[10/11] Running final-image DTB, UBI, metadata, ABI, capture, and privacy gates" | tee -a "$LOG"
python3 "$STAGE_DIR/verify_image.py" "$IMAGE_OUT" \
  --platform-sh "$OUT_DIR/platform.sh" \
  --package-manifest "$OUT_DIR/packages.manifest" \
  --module "$OUT_DIR/mt7915e.ko" \
  --kmod-package "$OUT_DIR/kmod-mt7915e.ipk" \
  --baseline-module "$OUT_DIR/mt7915e.vanilla.ko" \
  --baseline-kmod-package "$OUT_DIR/kmod-mt7915e.vanilla.ipk" \
  --kernel-release-file "$OUT_DIR/kernel.release" \
  --kernel-config "$OUT_DIR/kernel.config" \
  --module-symvers "$OUT_DIR/Module.symvers" \
  --ucert "$UCERT_HOST" \
  --usign "$USIGN_HOST" \
  --release-public-key "$PUBLIC_KEY" \
  --release-base-ucert "$BASE_UCERT" \
  --source-lock "$STAGE_DIR/source-lock.json" \
  --unsquashfs "$(command -v unsquashfs)" \
  --output "$OUT_DIR/gate-report.json" | tee "$OUT_DIR/gate-report.stdout.json"

echo "[11/11] Writing release hashes and provenance" | tee -a "$LOG"
install -m 0644 "$LOG" "$OUT_DIR/build.log"
python3 - "$OUT_DIR" "$SOURCE_DATE_EPOCH" "$STAGE_DIR" "$STAGE4_SOURCE_COMMIT" \
  "$STAGE4_SOURCE_TREE" "$STAGE4_SOURCE_ARCHIVE_SHA256" \
  "$BUILDER_IMAGE_ID" "$BUILDER_BASE_DIGEST" "$JOBS" <<'PY'
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[1])
gate = json.loads((out / "gate-report.json").read_text())
def h(path):
    x=hashlib.sha256(); x.update(path.read_bytes()); return x.hexdigest()
stage = Path(sys.argv[3])
source_lock = json.loads((stage / "source-lock.json").read_text())
builder_lock = source_lock["builder"]
tooling_files = (
    ".gitignore", "README.md", "BUILD.md", "PUBLIC-VS-PRIVATE.md", "RECOVERY.md", "RELEASE.md",
    "build-config.seed", "manifest.template.json", "source-lock.json", "verify_image.py",
    "keys/README.md",
    "container/Dockerfile", "container/apt-sources.list",
    "scripts/build_image.sh", "scripts/run_container_build.sh",
    "scripts/prepare_release_bundle.py", "scripts/verify_sources.py",
    "scripts/verify_vanilla_abi.py", "scripts/verify_capture_archive.py",
    "scripts/download_closure.py", "scripts/preflight_single_ubi.sh",
    "scripts/compare_repro_builds.py", "scripts/run_repro_pair.sh",
    "package/mtkcsi-dump/Makefile", "package/mtkcsi-dump/files/mtkcsi.config",
    "package/mtkcsi-dump/files/mtkcsi-dump.init",
    "patches/10-kwrt-vermagic-one.patch", "patches/23-ax3000t.patch",
    "patches/25-platform.patch", "patches/26-ax3000t-single-ubi-compat.patch",
    "patches/999-mt7915-csi-v2-hardened.patch",
    "tests/test_builder_lock.py", "tests/test_capture_gate.py", "tests/test_download_closure.py",
    "tests/test_fit_gate.py", "tests/test_metadata_gate.py",
    "tests/test_privacy_gate.py", "tests/test_tar_closure.py",
    "tests/test_release_bundle.py", "tests/test_repro_pair.py", "tests/test_wifi_gate.py",
)
missing = [name for name in tooling_files if not (stage / name).is_file()]
if missing:
    raise SystemExit(f"missing locked Stage4 tooling files: {missing}")
report_names = (
    "gate-report.json", "source-pristine-gates.json", "source-patched-gates.json",
    "capture-source-gates.json", "vanilla-abi-gates.json",
)
reports = {name: json.loads((out / name).read_text()) for name in report_names}
for name, report in reports.items():
    if report.get("result") != "pass" or not report.get("gates"):
        raise SystemExit(f"{name} is not a nonempty passing gate report")
image_name = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"
image_path = out / image_name
provenance = {
    "schema": 2,
    "classification": "EXPERIMENTAL-DO-NOT-FLASH",
    "publication_ready": False,
    "reproducibility_pending": True,
    "flash_authorized": False,
    "source_date_epoch": int(sys.argv[2]),
    "stage4_source_commit": sys.argv[4],
    "stage4_source_tree": sys.argv[5],
    "stage4_source_archive_sha256": sys.argv[6],
    "builder": {
        "base_digest": sys.argv[8],
        "image_id": sys.argv[7],
        "jobs": int(sys.argv[9]),
        "source_date_epoch": int(sys.argv[2]),
        "apt_snapshot": builder_lock["apt_snapshot"],
        "apt_snapshot_uri": builder_lock["apt_snapshot_uri"],
        "apt_archive_keyring_sha256": builder_lock["apt_archive_keyring_sha256"],
        "ca_bootstrap": builder_lock["ca_bootstrap"],
        "dockerfile_sha256": h(stage / "container/Dockerfile"),
        "apt_sources_sha256": h(stage / "container/apt-sources.list"),
        "package_versions_sha256": h(out / "builder-packages.txt"),
        "package_versions": (out / "builder-packages.txt").read_text().splitlines(),
        "networked_prepare_receipt_sha256": h(out / "network-prepare-receipt.json"),
        "download_manifest_sha256": h(out / "download-closure.json"),
        "build_network": "none",
    },
    "openwrt_commit": "f0a60eee2fe051741c643ea6118718aae1ef17fb",
    "openwrt_revision": "r33051-f5dae5ece4",
    "kwrt_layout_commit": "aae059682faae01d600db7061c150f65de87a21e",
    "kwrt_vermagic_transform_sha256": "43c09063907e10a9dd37a978be08ac5ea55774299162dd53a143963ecc1d57c5",
    "mt76_commit": "39c960c3ada558b4c2e7915772483d3731573d09",
    "image_identity": {
        "device": "xiaomi,mi-router-ax3000t",
        "compat_version": "2.0",
        "layout": "112 MiB single-UBI",
        "fixed_partitions_path": "/soc/spi@1100a000/flash@0/partitions",
        "compiled_partition_order": [
            "BL2", "Nvram", "Bdata", "Factory", "FIP", "crash", "crash_log", "KF", "ubi"
        ],
    },
    "gate_result": gate["result"],
    "image": {"name": image_name, "bytes": image_path.stat().st_size,
              "sha256": h(image_path)},
    "gate_report_sha256": h(out / "gate-report.json"),
    "signature": gate["artifacts"]["signature"],
    "audit_report_sha256": {name: h(out / name) for name in report_names},
    "tooling_sha256": {name: h(stage / name) for name in tooling_files},
    "capture_commit": "b8d7b73fc582795e734086a676a0a18a15980cb8",
    "capture_stage2_behavior_commit": "10adb198bc0a450e8906ac47f8dd4a14ab50c352",
    "capture_source_archive_bytes": 14026970,
    "capture_source_archive_sha256": "6f02ffbe03a1f5aaa491d1c32babad3595263356ac406f9cc38f64608a835a18",
    "capture_stage3_validation": {
        "pytest_passed": 86,
        "ctest_passed": 2,
        "ctest_total": 2,
        "sdist_exact_content_gate": True,
        "isolated_install_demo_byte_compare": True,
    },
    "capture_source_gate_sha256": h(out / "capture-source-gates.json"),
    "capture_default_enabled": False,
    "wireless_config_preseeded": False,
    "preserved_wireless_config_mutated": False,
    "runtime_board_detection_executed": False,
    "kernel_build_identity": "builder@buildhost",
    "kernel_package_format": "ipk",
    "kernel_dependency": "kernel (=6.12.94~1-r1)",
    "this_module_section_size": "0x440",
    "vanilla_undefined_symbols_count": 294,
    "vanilla_undefined_symbols_sha256": "a17a1bbec220f58147a40693cc8f1b1f8079b787f6eb7a9461eb9e4b352d10fb",
    "source_lock_sha256": h(out / "source-lock.json"),
    "kwrt_exact_config_sha256": h(out / "kwrt-exact.config"),
    "build_config_sha256": h(out / "build.config"),
    "kernel_config_sha256": h(out / "kernel.config"),
    "module_symvers_sha256": h(out / "Module.symvers"),
    "build_log_sha256": h(out / "build.log"),
    "package_manifest_sha256": h(out / "packages.manifest"),
}
(out / "build-provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True)+"\n")
PY

(
  cd "$OUT_DIR"
  sha256sum ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin \
    gate-report.json build-provenance.json > SHA256SUMS
  sha256sum ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin \
    packages.manifest mt7915e.ko kmod-mt7915e.ipk \
    mt7915e.vanilla.ko kmod-mt7915e.vanilla.ipk kernel.release platform.sh build.config \
    kernel.config Module.symvers kwrt-exact.config \
    source-lock.json source-pristine-gates.json source-patched-gates.json \
    vanilla-abi-gates.json capture-source-gates.json gate-report.json \
    build-provenance.json build.log builder-packages.txt network-prepare-receipt.json \
    download-closure.json ax3000t-stage4.pub ax3000t-stage4.ucert > AUDIT-SHA256SUMS
)

echo "Network-disabled build completed. Artifact remains EXPERIMENTAL—DO NOT FLASH."
