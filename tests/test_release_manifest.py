from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools.verify_stage3_sdist import (
    DELIVERY_MANIFEST,
    EXPECTED_GENERATED,
    GENERATED_METADATA,
    SDIST_ROOT,
    exact_source_allowlist,
    setuptools_sources_order,
    verify_delivery_manifest,
    verify_sdist,
)

REPOSITORY = Path(__file__).parents[1]


def _is_ignored(relative: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", relative],
        cwd=REPOSITORY,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(f"git check-ignore failed for {relative}")
    return result.returncode == 0


def test_public_sdist_uses_only_exact_regular_allowlisted_paths() -> None:
    allowed = exact_source_allowlist(REPOSITORY)
    assert "synthetic-demo/synthetic_result.json" in allowed
    assert "tests/fixtures/stage1_encoder_v2.csi2" in allowed
    assert not any(path.endswith((".pem", ".key", ".p12")) for path in allowed)
    assert not any("private" in path.lower() for path in allowed)


def test_delivery_manifest_exactly_covers_and_hashes_public_sources() -> None:
    verify_delivery_manifest(REPOSITORY)


def _copy_public_repository(destination: Path) -> None:
    for relative in exact_source_allowlist(REPOSITORY):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY / relative, target)


def _canonical_sdist_payloads() -> dict[str, bytes]:
    sources = exact_source_allowlist(REPOSITORY)
    payloads = {relative: (REPOSITORY / relative).read_bytes() for relative in sources}
    payloads["setup.cfg"] += b"\n[egg_info]\ntag_build = \ntag_date = 0\n\n"
    generated = dict(EXPECTED_GENERATED)
    generated["ax3000t_csi_localization.egg-info/PKG-INFO"] = generated["PKG-INFO"]
    source_listing = setuptools_sources_order(
        sources | (GENERATED_METADATA - {"PKG-INFO"})
    )
    generated["ax3000t_csi_localization.egg-info/SOURCES.txt"] = "\n".join(
        source_listing
    ).encode()
    payloads.update(generated)
    return payloads


def _normalized_tar_info(name: str, *, directory: bool) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.uid = 1
    member.gid = 1
    member.uname = "daemon"
    member.gname = "daemon"
    member.mtime = 1_700_000_000
    if directory:
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
    else:
        member.mode = 0o644
    return member


def _write_canonical_sdist(
    archive: Path,
    *,
    payload_overrides: dict[str, bytes] | None = None,
    extra_directories: tuple[str, ...] = (),
    secret_pax_path: str | None = None,
) -> None:
    payloads = _canonical_sdist_payloads()
    payloads.update(payload_overrides or {})
    directories = {
        parent.as_posix()
        for relative in payloads
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        for relative in ("", *sorted(directories), *extra_directories):
            name = SDIST_ROOT if not relative else f"{SDIST_ROOT}/{relative}"
            bundle.addfile(_normalized_tar_info(name, directory=True))
        for relative, payload in sorted(payloads.items()):
            member = _normalized_tar_info(f"{SDIST_ROOT}/{relative}", directory=False)
            member.size = len(payload)
            if relative == secret_pax_path:
                member.pax_headers = {"comment": "private-host-identity"}
            bundle.addfile(member, io.BytesIO(payload))


@pytest.mark.parametrize("attack", ["missing", "duplicate", "tamper"])
def test_delivery_manifest_rejects_incomplete_or_false_closure(
    tmp_path: Path, attack: str
) -> None:
    repository = tmp_path / "repository"
    _copy_public_repository(repository)
    manifest = repository / DELIVERY_MANIFEST
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if attack == "missing":
        manifest.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    elif attack == "duplicate":
        manifest.write_text("\n".join([*lines, lines[0]]) + "\n", encoding="utf-8")
    else:
        (repository / "README.md").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="manifest|unsafe/duplicate"):
        verify_delivery_manifest(repository)


def test_artifact_directories_deny_private_files_by_default() -> None:
    private_hypothetical_paths = (
        "synthetic-demo/private-real.csi2",
        "synthetic-demo/real_session.json",
        "synthetic-demo/private.pem",
        "tests/fixtures/private.csi2",
        "tests/fixtures/private.pem",
        "examples/real_session.json",
        "session.json",
        "target-session.json",
        "calibration.json",
        "target-calibration.json",
        "target-aoa.json",
        "target-cir.json",
        "target-range-model.json",
    )
    public_exact_paths = (
        "synthetic-demo/synthetic_result.json",
        "tests/fixtures/stage1_encoder_v2.csi2",
        "examples/chain_mapping.example.json",
    )
    assert all(_is_ignored(path) for path in private_hypothetical_paths)
    assert all(not _is_ignored(path) for path in public_exact_paths)


def test_sdist_verifier_rejects_duplicate_archive_members(tmp_path) -> None:
    archive = tmp_path / "duplicate.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for payload in (b"first", b"second"):
            member = _normalized_tar_info("package/README.md", directory=False)
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
    with pytest.raises(ValueError, match="duplicate public archive member"):
        verify_sdist(archive, REPOSITORY)


def test_sdist_verifier_rejects_forged_allowlisted_payload(tmp_path) -> None:
    archive = tmp_path / "forged.tar.gz"
    _write_canonical_sdist(
        archive, payload_overrides={"README.md": b"FORGED-PAYLOAD\n"}
    )
    with pytest.raises(ValueError, match="source content mismatch: README.md"):
        verify_sdist(archive, REPOSITORY)


def test_sdist_verifier_rejects_extra_empty_directory(tmp_path) -> None:
    archive = tmp_path / "extra-directory.tar.gz"
    _write_canonical_sdist(archive, extra_directories=("private-empty",))
    with pytest.raises(ValueError, match="directory closure mismatch"):
        verify_sdist(archive, REPOSITORY)


def test_sdist_verifier_rejects_unapproved_pax_metadata(tmp_path) -> None:
    archive = tmp_path / "secret-pax.tar.gz"
    _write_canonical_sdist(archive, secret_pax_path="README.md")
    with pytest.raises(ValueError, match="unapproved PAX metadata"):
        verify_sdist(archive, REPOSITORY)


def test_source_allowlist_rejects_symlinked_ancestor(tmp_path) -> None:
    repository = tmp_path / "repository"
    _copy_public_repository(repository)
    shutil.rmtree(repository / "localization")
    (repository / "localization").symlink_to(
        REPOSITORY / "localization", target_is_directory=True
    )
    with pytest.raises(ValueError, match="uses a symlink"):
        exact_source_allowlist(repository)
