#!/usr/bin/env python3
"""Hard-gate two independent clean Stage4 builds and finalize one canonical output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


IMAGE = "ax3000t-112m-csi-25.12.5-experimental-sysupgrade.bin"
COMPARE_FILES = (
    IMAGE, "packages.manifest", "mt7915e.ko", "kmod-mt7915e.ipk",
    "mt7915e.vanilla.ko", "kmod-mt7915e.vanilla.ipk", "kernel.release",
    "kernel.config", "Module.symvers", "platform.sh", "build.config",
    "kwrt-exact.config", "source-lock.json", "source-pristine-gates.json",
    "source-patched-gates.json", "vanilla-abi-gates.json",
    "capture-source-gates.json", "gate-report.json", "builder-packages.txt",
    "network-prepare-receipt.json", "download-closure.json",
    "ax3000t-stage4.pub", "ax3000t-stage4.ucert",
)
AUDIT_FILES = COMPARE_FILES + (
    "build-provenance.json", "build.log", "reproducibility-gates.json",
)


def regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_sums(path: Path, directory: Path, names: tuple[str, ...]) -> None:
    path.write_text("".join(f"{digest(directory / name)}  {name}\n" for name in names))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", required=True, type=Path,
                        help="first clean build; becomes canonical after PASS")
    parser.add_argument("--second", required=True, type=Path,
                        help="independent second clean build")
    args = parser.parse_args()
    if args.first.is_symlink() or args.second.is_symlink():
        print("reproducibility gate rejected symlinked build directory", file=sys.stderr)
        return 1
    first, second = args.first.resolve(), args.second.resolve()
    if first == second or not first.is_dir() or not second.is_dir():
        print("reproducibility gate requires two distinct build directories", file=sys.stderr)
        return 1
    missing = [
        f"{label}:{name}" for label, directory in (("first", first), ("second", second))
        for name in COMPARE_FILES if not regular(directory / name)
    ]
    if missing:
        print(f"reproducibility gate missing regular inputs: {missing}", file=sys.stderr)
        return 1

    gates = []
    for name in COMPARE_FILES:
        first_hash, second_hash = digest(first / name), digest(second / name)
        gates.append({
            "name": f"repro.byte_identity.{name}",
            "status": "pass" if first_hash == second_hash else "fail",
            "detail": "independent clean builds are byte-identical for this artifact",
            "evidence": {"first_sha256": first_hash, "second_sha256": second_hash},
        })
    provenance_values = []
    for directory in (first, second):
        try:
            provenance = json.loads((directory / "build-provenance.json").read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"invalid pre-repro provenance: {exc}", file=sys.stderr)
            return 1
        provenance_values.append({
            "stage4_source_commit": provenance.get("stage4_source_commit"),
            "stage4_source_tree": provenance.get("stage4_source_tree"),
            "stage4_source_archive_sha256": provenance.get(
                "stage4_source_archive_sha256"
            ),
            "builder": provenance.get("builder", {}).get("image_id"),
            "base": provenance.get("builder", {}).get("base_digest"),
            "apt_snapshot": provenance.get("builder", {}).get("apt_snapshot"),
            "apt_snapshot_uri": provenance.get("builder", {}).get("apt_snapshot_uri"),
            "apt_archive_keyring_sha256": provenance.get("builder", {}).get(
                "apt_archive_keyring_sha256"
            ),
            "dockerfile_sha256": provenance.get("builder", {}).get("dockerfile_sha256"),
            "apt_sources_sha256": provenance.get("builder", {}).get("apt_sources_sha256"),
            "package_versions_sha256": provenance.get("builder", {}).get(
                "package_versions_sha256"
            ),
            "source_date_epoch": provenance.get("source_date_epoch"),
        })
    provenance_ok = (
        provenance_values[0] == provenance_values[1] and
        re.fullmatch(r"[0-9a-f]{40}", str(provenance_values[0]["stage4_source_commit"] or ""))
        is not None
    )
    gates.append({
        "name": "repro.provenance.build_identity",
        "status": "pass" if provenance_ok else "fail",
        "detail": "both clean builds use one exact source commit, builder image, base, and epoch",
        "evidence": provenance_values,
    })
    result = "pass" if all(gate["status"] == "pass" for gate in gates) else "fail"
    report = {
        "schema": 1,
        "classification": "EXPERIMENTAL-DO-NOT-FLASH",
        "result": result,
        "clean_build_count": 2,
        "image": IMAGE,
        "image_sha256": digest(first / IMAGE),
        "gates": gates,
    }
    report_path = first / "reproducibility-gates.json"
    if report_path.is_symlink():
        print("refusing symlinked reproducibility report path", file=sys.stderr)
        return 1
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if result != "pass":
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    provenance_path = first / "build-provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance.update({
        "publication_ready": True,
        "reproducibility_pending": False,
        "reproducibility_clean_builds": 2,
        "reproducibility_gate_sha256": digest(report_path),
        "reproducibility_second_image_sha256": digest(second / IMAGE),
    })
    report_hashes = provenance.get("audit_report_sha256")
    if not isinstance(report_hashes, dict):
        print("canonical provenance lacks audit report hash map", file=sys.stderr)
        return 1
    report_hashes["reproducibility-gates.json"] = digest(report_path)
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    write_sums(first / "SHA256SUMS", first,
               (IMAGE, "gate-report.json", "build-provenance.json"))
    write_sums(first / "AUDIT-SHA256SUMS", first, AUDIT_FILES)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
