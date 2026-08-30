"""Authorized UDP-v2 capture recorder with explicit provenance."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .contracts import RadioToneConfig, validate_analysis_record
from .csi2 import decode_csi2_datagram
from .jsonio import dumps_json
from .models import CSIRecord, normalize_mac
from .session import SequenceStats, SessionManifest


@dataclass(frozen=True)
class RecorderMetadata:
    session_id: str
    receiver_id: str
    router_model: str
    interface: str
    boot_id: str
    radio_epoch: str
    timebase_id: str
    clock_uncertainty_ns: int
    driver_commit: str
    source_tree_hash: str


class CSI2CaptureRecorder:
    """Validate and frame datagrams; suitable for deterministic unit tests."""

    def __init__(
        self,
        output: BinaryIO,
        *,
        sender_allowlist: set[tuple[str, int]],
        transmitter_allowlist: set[str],
    ) -> None:
        if not sender_allowlist or not transmitter_allowlist:
            raise ValueError("sender and transmitter allowlists must be non-empty")
        for host, port in sender_allowlist:
            ipaddress.ip_address(host)
            if type(port) is not int or not 1 <= port <= 65_535:
                raise ValueError("sender source ports must be in 1..65535")
        self.output = output
        self.sender_allowlist = set(sender_allowlist)
        self.transmitter_allowlist = {
            normalize_mac(value) for value in transmitter_allowlist
        }
        self.radio_config: RadioToneConfig | None = None
        self.transmitter_address: str | None = None
        self.start_ns: int | None = None
        self.end_ns: int | None = None
        self.accepted = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.estimated_lost = 0
        self.first_sequence: int | None = None
        self.last_sequence: int | None = None

    def ingest(
        self, sender_endpoint: tuple[str, int], datagram: bytes
    ) -> CSIRecord | None:
        if sender_endpoint not in self.sender_allowlist:
            raise ValueError("UDP sender IP/source-port is outside the allowlist")
        record = decode_csi2_datagram(datagram)
        config = validate_analysis_record(record)
        if record.transmitter_address not in self.transmitter_allowlist:
            raise ValueError("CSI transmitter is outside the explicit allowlist")
        if self.transmitter_address is None:
            self.transmitter_address = record.transmitter_address
        elif record.transmitter_address != self.transmitter_address:
            raise ValueError("one capture session may contain only one transmitter")
        if self.radio_config is None:
            self.radio_config = config
        elif config.signature() != self.radio_config.signature():
            raise ValueError("radio/tone configuration changed during capture")

        if self.last_sequence is not None:
            delta = (record.sequence - self.last_sequence) & 0xFFFF_FFFF
            if delta == 0:
                self.duplicates += 1
                return None
            if delta >= 0x8000_0000:
                self.out_of_order += 1
                return None
            self.estimated_lost += max(0, delta - 1)
        else:
            self.first_sequence = record.sequence
        self.last_sequence = record.sequence
        self.output.write(struct.pack(">I", len(datagram)))
        self.output.write(datagram)
        self.accepted += 1
        self.start_ns = (
            record.host_timestamp_ns
            if self.start_ns is None
            else min(self.start_ns, record.host_timestamp_ns)
        )
        self.end_ns = (
            record.host_timestamp_ns
            if self.end_ns is None
            else max(self.end_ns, record.host_timestamp_ns)
        )
        return record

    @property
    def sequence_stats(self) -> SequenceStats:
        return SequenceStats(
            accepted_datagrams=self.accepted,
            duplicate_datagrams=self.duplicates,
            out_of_order_datagrams=self.out_of_order,
            estimated_lost_datagrams=self.estimated_lost,
            first_sequence=self.first_sequence,
            last_sequence=self.last_sequence,
        )


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _validate_bound_directory(descriptor: int, *, output_parent: bool) -> None:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("output path component is not a directory")
    if hasattr(os, "geteuid"):
        allowed_owners = {0, os.geteuid()}
        if status.st_uid not in allowed_owners:
            raise PermissionError(
                "output path components must be owned by root or the recorder uid"
            )
    writable_by_others = stat.S_IMODE(status.st_mode) & 0o022
    sticky = status.st_mode & stat.S_ISVTX
    if writable_by_others and (output_parent or not sticky):
        raise PermissionError(
            "output path components must not permit unsafe rename/write races"
        )


def _open_private_parent(path: Path, *, create: bool = True) -> tuple[int, str]:
    """Open/create a private output parent without following path symlinks.

    The returned descriptor stays bound to the selected directory even if one
    of its names is concurrently renamed.  Sensitive CSI output is refused in
    a directory writable by another uid because path-only no-clobber checks
    cannot protect against an attacker replacing a file after creation.
    """

    if not path.name or ".." in path.parts:
        raise ValueError("output paths must have a basename and contain no '..'")
    absolute = path if path.is_absolute() else Path.cwd() / path
    components = absolute.parent.parts
    if absolute.is_absolute():
        descriptor = os.open(os.path.sep, _directory_flags())
        components = components[1:]
    else:  # pragma: no cover - Path.cwd() makes this unreachable on CPython
        descriptor = os.open(".", _directory_flags())
    try:
        _validate_bound_directory(descriptor, output_parent=False)
        for component in components:
            if component in {"", "."}:
                continue
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(component, _directory_flags(), dir_fd=descriptor)
            try:
                _validate_bound_directory(next_descriptor, output_parent=False)
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        status = os.fstat(descriptor)
        if hasattr(os, "geteuid") and status.st_uid != os.geteuid():
            raise PermissionError("output parent must be owned by the recorder uid")
        _validate_bound_directory(descriptor, output_parent=True)
        return descriptor, path.name
    except Exception:
        os.close(descriptor)
        raise


def _path_entry_exists(directory_fd: int, basename: str) -> bool:
    """Return true for files, directories, and dangling symbolic links."""

    try:
        os.stat(basename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_private_exclusive(directory_fd: int, basename: str) -> int:
    """Create one private regular file relative to a bound parent fd."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(basename, flags, 0o600, dir_fd=directory_fd)
    os.fchmod(descriptor, 0o600)
    return descriptor


def _unlink_at(directory_fd: int, basename: str) -> None:
    try:
        os.unlink(basename, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _fsync_directory(directory_fd: int) -> None:
    os.fsync(directory_fd)


def _sha256_at(directory_fd: int, basename: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(basename, flags, dir_fd=directory_fd)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("sealed capture is not a regular file")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _verify_published_path(
    path: Path,
    bound_directory_fd: int,
    basename: str,
    expected_sha256: str,
) -> None:
    """Prove the user-visible name still resolves to the bound sealed inode."""

    reopened_fd, reopened_basename = _open_private_parent(path, create=False)
    try:
        if reopened_basename != basename:
            raise RuntimeError("published output basename changed")
        bound_directory = os.fstat(bound_directory_fd)
        reopened_directory = os.fstat(reopened_fd)
        if (bound_directory.st_dev, bound_directory.st_ino) != (
            reopened_directory.st_dev,
            reopened_directory.st_ino,
        ):
            raise RuntimeError("published output parent changed during capture")
        bound_file = os.stat(basename, dir_fd=bound_directory_fd, follow_symlinks=False)
        reopened_file = os.stat(basename, dir_fd=reopened_fd, follow_symlinks=False)
        if not stat.S_ISREG(bound_file.st_mode) or (
            bound_file.st_dev,
            bound_file.st_ino,
        ) != (reopened_file.st_dev, reopened_file.st_ino):
            raise RuntimeError("published output name is not the sealed regular file")
        if _sha256_at(reopened_fd, basename) != expected_sha256:
            raise RuntimeError("published output bytes changed during sealing")
    finally:
        os.close(reopened_fd)


def record_udp_session(
    *,
    router_host: str,
    router_port: int,
    listen_host: str,
    listen_port: int,
    capture_path: str | Path,
    manifest_path: str | Path,
    metadata: RecorderMetadata,
    sender_allowlist: set[tuple[str, int]],
    transmitter_allowlist: set[str],
    duration_s: float,
    maximum_packets: int,
) -> SessionManifest:
    """Send ``register-v2``, record validated datagrams, and seal a manifest."""

    if duration_s <= 0 or maximum_packets <= 0:
        raise ValueError("duration and maximum_packets must be positive")
    ipaddress.ip_address(router_host)
    if (router_host, router_port) not in sender_allowlist:
        raise ValueError("router destination must be in the sender endpoint allowlist")
    capture = Path(capture_path)
    manifest_destination = Path(manifest_path)
    if capture.absolute() == manifest_destination.absolute():
        raise ValueError("capture, manifest, and both partial paths must be distinct")
    if capture.suffix != ".csi2f":
        raise ValueError("capture path must use the .csi2f suffix")
    if manifest_destination.suffix != ".json":
        raise ValueError("manifest path must use the .json suffix")
    capture_directory_fd, capture_basename = _open_private_parent(capture)
    try:
        manifest_directory_fd, manifest_basename = _open_private_parent(
            manifest_destination
        )
    except Exception:
        os.close(capture_directory_fd)
        raise
    parent_directories_open = True

    def close_parent_directories() -> None:
        nonlocal parent_directories_open
        if not parent_directories_open:
            return
        parent_directories_open = False
        try:
            os.close(capture_directory_fd)
        finally:
            os.close(manifest_directory_fd)

    try:
        capture_partial_basename = capture_basename + ".partial"
        manifest_partial_basename = manifest_basename + ".partial"
        capture_directory_identity = os.fstat(capture_directory_fd)
        manifest_directory_identity = os.fstat(manifest_directory_fd)
        target_identities = {
            (
                capture_directory_identity.st_dev,
                capture_directory_identity.st_ino,
                capture_basename,
            ),
            (
                manifest_directory_identity.st_dev,
                manifest_directory_identity.st_ino,
                manifest_basename,
            ),
            (
                capture_directory_identity.st_dev,
                capture_directory_identity.st_ino,
                capture_partial_basename,
            ),
            (
                manifest_directory_identity.st_dev,
                manifest_directory_identity.st_ino,
                manifest_partial_basename,
            ),
        }
        if len(target_identities) != 4:
            raise ValueError(
                "capture, manifest, and both partial paths must be distinct"
            )
        targets = (
            (capture_directory_fd, capture_basename),
            (manifest_directory_fd, manifest_basename),
            (capture_directory_fd, capture_partial_basename),
            (manifest_directory_fd, manifest_partial_basename),
        )
        if any(
            _path_entry_exists(directory_fd, basename)
            for directory_fd, basename in targets
        ):
            raise FileExistsError(
                "refusing to overwrite an existing capture, manifest, or partial"
            )
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception:
        close_parent_directories()
        raise
    recorder: CSI2CaptureRecorder | None = None
    capture_partial_owned = False
    capture_owned = False
    manifest_partial_owned = False
    manifest_owned = False
    try:
        sock.bind((listen_host, listen_port))
        sock.settimeout(min(1.0, duration_s))
        sock.sendto(b"register-v2", (router_host, router_port))
        deadline = time.monotonic() + duration_s
        file_descriptor = _open_private_exclusive(
            capture_directory_fd, capture_partial_basename
        )
        capture_partial_owned = True
        with os.fdopen(file_descriptor, "wb") as output:
            recorder = CSI2CaptureRecorder(
                output,
                sender_allowlist=sender_allowlist,
                transmitter_allowlist=transmitter_allowlist,
            )
            while time.monotonic() < deadline and recorder.accepted < maximum_packets:
                try:
                    datagram, address = sock.recvfrom(65_535)
                except TimeoutError:
                    continue
                recorder.ingest((address[0], address[1]), datagram)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            if capture_partial_owned:
                _unlink_at(capture_directory_fd, capture_partial_basename)
        finally:
            close_parent_directories()
        raise
    finally:
        try:
            sock.close()
        except Exception:
            close_parent_directories()
            raise
    if recorder is None or recorder.accepted == 0 or recorder.radio_config is None:
        try:
            if capture_partial_owned:
                _unlink_at(capture_directory_fd, capture_partial_basename)
        finally:
            close_parent_directories()
        raise ValueError("capture contains no accepted CSI2 datagrams")
    try:
        os.link(
            capture_partial_basename,
            capture_basename,
            src_dir_fd=capture_directory_fd,
            dst_dir_fd=capture_directory_fd,
            follow_symlinks=False,
        )
        capture_owned = True
        os.unlink(capture_partial_basename, dir_fd=capture_directory_fd)
        capture_partial_owned = False
        _fsync_directory(capture_directory_fd)
    except Exception:
        try:
            if capture_partial_owned:
                _unlink_at(capture_directory_fd, capture_partial_basename)
            if capture_owned:
                _unlink_at(capture_directory_fd, capture_basename)
        finally:
            close_parent_directories()
        raise
    try:
        capture_digest = _sha256_at(capture_directory_fd, capture_basename)
        manifest = SessionManifest(
            session_id=metadata.session_id,
            receiver_id=metadata.receiver_id,
            router_model=metadata.router_model,
            interface=metadata.interface,
            boot_id=metadata.boot_id,
            radio_epoch=metadata.radio_epoch,
            timebase_id=metadata.timebase_id,
            clock_uncertainty_ns=metadata.clock_uncertainty_ns,
            driver_commit=metadata.driver_commit,
            source_tree_hash=metadata.source_tree_hash,
            capture_file=capture_basename,
            capture_sha256=capture_digest,
            start_host_timestamp_ns=int(recorder.start_ns),
            end_host_timestamp_ns=int(recorder.end_ns),
            radio_config=recorder.radio_config,
            sender_allowlist=tuple(
                f"{host}:{port}" for host, port in sorted(sender_allowlist)
            ),
            transmitter_allowlist=(str(recorder.transmitter_address),),
            sequence_stats=recorder.sequence_stats,
        )
        payload = (dumps_json(manifest.to_dict()) + "\n").encode("utf-8")
        manifest_descriptor = _open_private_exclusive(
            manifest_directory_fd, manifest_partial_basename
        )
        manifest_partial_owned = True
        with os.fdopen(manifest_descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(
            manifest_partial_basename,
            manifest_basename,
            src_dir_fd=manifest_directory_fd,
            dst_dir_fd=manifest_directory_fd,
            follow_symlinks=False,
        )
        manifest_owned = True
        os.unlink(manifest_partial_basename, dir_fd=manifest_directory_fd)
        manifest_partial_owned = False
        _fsync_directory(manifest_directory_fd)
        _verify_published_path(
            capture,
            capture_directory_fd,
            capture_basename,
            capture_digest,
        )
        _verify_published_path(
            manifest_destination,
            manifest_directory_fd,
            manifest_basename,
            hashlib.sha256(payload).hexdigest(),
        )
    except Exception:
        if capture_owned:
            _unlink_at(capture_directory_fd, capture_basename)
        if manifest_partial_owned:
            _unlink_at(manifest_directory_fd, manifest_partial_basename)
        if manifest_owned:
            _unlink_at(manifest_directory_fd, manifest_basename)
        raise
    finally:
        close_parent_directories()
    return manifest
