"""Fail closed unless a Stage-3 sdist contains exactly the public allowlist."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

GENERATED_METADATA = {
    "PKG-INFO",
    "ax3000t_csi_localization.egg-info/PKG-INFO",
    "ax3000t_csi_localization.egg-info/SOURCES.txt",
    "ax3000t_csi_localization.egg-info/dependency_links.txt",
    "ax3000t_csi_localization.egg-info/entry_points.txt",
    "ax3000t_csi_localization.egg-info/requires.txt",
    "ax3000t_csi_localization.egg-info/top_level.txt",
}
ALLOWED_NON_INCLUDE_DIRECTIVES = {
    "exclude tests/test_capture.cpp",
    "exclude tests/test_csi_protocol.py",
    "prune .venv",
    "prune .pytest_cache",
    "prune .ruff_cache",
    "global-exclude __pycache__ *.py[cod] *.egg-info",
}
DELIVERY_MANIFEST = "STAGE3_DELIVERY_MANIFEST.sha256"
DELIVERY_LINE = re.compile(r"([0-9a-f]{64})  ([^\r\n]+)")
SDIST_ROOT = "ax3000t_csi_localization-0.1.0"
EXPECTED_GENERATED = {
    "PKG-INFO": (
        b"Metadata-Version: 2.4\n"
        b"Name: ax3000t-csi-localization\n"
        b"Version: 0.1.0\n"
        b"Summary: Evidence-gated coarse localization experiments for "
        b"AX3000T CSI2 captures\n"
        b"Requires-Python: >=3.10\n"
        b"License-File: LICENSE.txt\n"
        b"Requires-Dist: numpy>=1.24\n"
        b"Provides-Extra: test\n"
        b'Requires-Dist: build==1.6.0; extra == "test"\n'
        b'Requires-Dist: pytest>=8; extra == "test"\n'
        b'Requires-Dist: ruff==0.16.5; extra == "test"\n'
        b"Dynamic: license-file\n"
    ),
    "ax3000t_csi_localization.egg-info/dependency_links.txt": b"\n",
    "ax3000t_csi_localization.egg-info/entry_points.txt": (
        b"[console_scripts]\nax3000t-localize = localization.cli:main\n"
    ),
    "ax3000t_csi_localization.egg-info/requires.txt": (
        b"numpy>=1.24\n\n[test]\nbuild==1.6.0\npytest>=8\nruff==0.16.5\n"
    ),
    "ax3000t_csi_localization.egg-info/top_level.txt": b"localization\n",
}


def exact_source_allowlist(repository: Path) -> set[str]:
    manifest = repository / "MANIFEST.in"
    allowed: set[str] = set()
    seen_other: set[str] = set()
    for raw_line in manifest.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("include "):
            value = line.removeprefix("include ")
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or ".." in path.parts
                or any(character in value for character in "*?[]\\")
            ):
                raise ValueError(
                    f"MANIFEST include is not one exact safe path: {value}"
                )
            if value in allowed:
                raise ValueError(f"MANIFEST contains a duplicate include: {value}")
            allowed.add(value)
        else:
            seen_other.add(line)
    if seen_other != ALLOWED_NON_INCLUDE_DIRECTIVES:
        raise ValueError(
            "MANIFEST contains unapproved non-exact directives: "
            + repr(sorted(seen_other))
        )
    if not allowed:
        raise ValueError("public source allowlist is empty")
    for relative in sorted(allowed):
        source = repository
        parts = PurePosixPath(relative).parts
        for index, part in enumerate(parts):
            source = source / part
            try:
                status = source.lstat()
            except FileNotFoundError as error:
                raise ValueError(
                    f"allowlisted source is missing/non-regular: {relative}"
                ) from error
            if stat.S_ISLNK(status.st_mode):
                raise ValueError(f"allowlisted source uses a symlink: {relative}")
            if index < len(parts) - 1 and not stat.S_ISDIR(status.st_mode):
                raise ValueError(
                    f"allowlisted source ancestor is not a directory: {relative}"
                )
            if index == len(parts) - 1 and not stat.S_ISREG(status.st_mode):
                raise ValueError(
                    f"allowlisted source is missing/non-regular: {relative}"
                )
    return allowed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def setuptools_sources_order(paths: set[str]) -> list[str]:
    """Reproduce setuptools FileList's files-before-subdirectories ordering."""

    result: list[str] = []

    def visit(directory: PurePosixPath) -> None:
        direct_files: list[str] = []
        child_directories: set[str] = set()
        for relative in paths:
            path = PurePosixPath(relative)
            if path.parent == directory:
                direct_files.append(relative)
                continue
            try:
                remainder = path.relative_to(directory)
            except ValueError:
                continue
            if len(remainder.parts) > 1:
                child_directories.add(remainder.parts[0])
        result.extend(sorted(direct_files))
        for child in sorted(child_directories):
            visit(directory / child)

    visit(PurePosixPath("."))
    if set(result) != paths or len(result) != len(paths):
        raise AssertionError("source-list ordering failed to cover every path")
    return result


def verify_delivery_manifest(repository: Path) -> None:
    expected = exact_source_allowlist(repository) - {DELIVERY_MANIFEST}
    manifest = repository / DELIVERY_MANIFEST
    actual: dict[str, str] = {}
    for line_number, line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = DELIVERY_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid delivery manifest line {line_number}")
        digest, relative = match.groups()
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or relative in actual
        ):
            raise ValueError(f"unsafe/duplicate delivery manifest path: {relative}")
        actual[relative] = digest
    if set(actual) != expected:
        raise ValueError(
            "delivery manifest allowlist mismatch; "
            f"missing={sorted(expected - set(actual))}, "
            f"unexpected={sorted(set(actual) - expected)}"
        )
    if list(actual) != sorted(actual):
        raise ValueError("delivery manifest paths are not sorted")
    for relative, expected_digest in actual.items():
        observed_digest = _sha256_file(repository / relative)
        if observed_digest != expected_digest:
            raise ValueError(f"delivery manifest hash mismatch: {relative}")


def verify_sdist(archive: Path, repository: Path) -> None:
    source_paths = exact_source_allowlist(repository)
    expected = source_paths | GENERATED_METADATA
    expected_directories = {
        parent.as_posix()
        for relative in expected
        for parent in PurePosixPath(relative).parents
        if parent.as_posix() != "."
    }
    expected_directories.add("")
    actual: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    roots: set[str] = set()
    with tarfile.open(archive, mode="r:gz") as bundle:
        if bundle.pax_headers:
            raise ValueError("global PAX metadata is not allowed")
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            canonical_name = path.as_posix()
            if (
                path.is_absolute()
                or ".." in path.parts
                or len(path.parts) < 1
                or "\\" in member.name
                or canonical_name != member.name.rstrip("/")
            ):
                raise ValueError(f"unsafe archive member path: {member.name}")
            roots.add(path.parts[0])
            if member.uid != 1 or member.gid != 1:
                raise ValueError(f"archive owner IDs are not normalized: {member.name}")
            if member.uname != "daemon" or member.gname != "daemon":
                raise ValueError(
                    f"archive owner names are not normalized: {member.name}"
                )
            if set(member.pax_headers) - {"mtime"}:
                raise ValueError(f"unapproved PAX metadata: {member.name}")
            pax_mtime = member.pax_headers.get("mtime")
            if (
                pax_mtime is not None
                and re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,9})?", pax_mtime) is None
            ):
                raise ValueError(f"invalid PAX mtime metadata: {member.name}")
            if member.linkname or member.devmajor or member.devminor:
                raise ValueError(f"link/device metadata is not allowed: {member.name}")
            if member.issparse():
                raise ValueError(
                    f"sparse archive members are not allowed: {member.name}"
                )
            if member.isdir():
                relative_directory = PurePosixPath(*path.parts[1:]).as_posix()
                if relative_directory == ".":
                    relative_directory = ""
                if relative_directory in actual_directories:
                    raise ValueError(
                        f"duplicate public archive directory: {relative_directory}"
                    )
                if member.mode != 0o755:
                    raise ValueError(
                        f"archive directory mode is not 0755: {member.name}"
                    )
                actual_directories.add(relative_directory)
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f"non-regular public archive member: {member.name}")
            if len(path.parts) < 2:
                raise ValueError(f"file is outside the sdist root: {member.name}")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in actual:
                raise ValueError(f"duplicate public archive member: {relative}")
            if member.mode != 0o644:
                raise ValueError(f"archive file mode is not 0644: {member.name}")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise ValueError(f"cannot read archive member: {member.name}")
            payload = extracted.read()
            if len(payload) != member.size:
                raise ValueError(f"archive member size mismatch: {member.name}")
            actual[relative] = payload
    if roots != {SDIST_ROOT}:
        raise ValueError(f"sdist root mismatch, got {sorted(roots)}")
    missing = sorted(expected - set(actual))
    unexpected = sorted(set(actual) - expected)
    if missing or unexpected:
        raise ValueError(
            f"sdist allowlist mismatch; missing={missing}, unexpected={unexpected}"
        )
    missing_directories = sorted(expected_directories - actual_directories)
    unexpected_directories = sorted(actual_directories - expected_directories)
    if missing_directories or unexpected_directories:
        raise ValueError(
            "sdist directory closure mismatch; "
            f"missing={missing_directories}, unexpected={unexpected_directories}"
        )
    for relative in sorted(source_paths):
        source_payload = (repository / relative).read_bytes()
        if relative == "setup.cfg":
            source_payload += b"\n[egg_info]\ntag_build = \ntag_date = 0\n\n"
        if actual[relative] != source_payload:
            raise ValueError(f"sdist source content mismatch: {relative}")
    expected_generated = dict(EXPECTED_GENERATED)
    expected_generated["ax3000t_csi_localization.egg-info/PKG-INFO"] = (
        EXPECTED_GENERATED["PKG-INFO"]
    )
    expected_sources = setuptools_sources_order(
        source_paths | (GENERATED_METADATA - {"PKG-INFO"})
    )
    expected_generated["ax3000t_csi_localization.egg-info/SOURCES.txt"] = "\n".join(
        expected_sources
    ).encode("utf-8")
    if set(expected_generated) != GENERATED_METADATA:
        raise AssertionError("generated metadata verifier is incomplete")
    for relative, expected_payload in sorted(expected_generated.items()):
        if actual[relative] != expected_payload:
            raise ValueError(f"generated metadata content mismatch: {relative}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repository = args.repository.resolve()
    verify_delivery_manifest(repository)
    verify_sdist(args.archive, repository)
    print("stage3_sdist_allowlist=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
