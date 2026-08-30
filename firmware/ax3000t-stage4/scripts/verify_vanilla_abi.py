#!/usr/bin/env python3
"""Fail fast unless a vanilla mt7915e rebuild matches the live Kwrt ABI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_image import Report, verify_kmod_package, verify_module  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--ipk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.is_symlink():
        print("refusing to overwrite a symlinked ABI report", file=sys.stderr)
        return 2

    report = Report(args.ipk)
    verify_module(args.module, report, baseline=True)
    verify_kmod_package(args.ipk, args.module, report, baseline=True)
    data = report.as_json()
    data["purpose"] = "vanilla-before-CSI ABI gate"
    output = json.dumps(data, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
