"""Capture-session manifest and provenance compatibility gates."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import RadioToneConfig, validate_analysis_record
from .jsonio import dump_json, json_safe, load_json
from .models import CSIRecord, normalize_mac


def _strict_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a JSON integer")
    return value


def _strict_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a JSON string")
    return value


@dataclass(frozen=True)
class SequenceStats:
    accepted_datagrams: int = 0
    duplicate_datagrams: int = 0
    out_of_order_datagrams: int = 0
    estimated_lost_datagrams: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None

    def __post_init__(self) -> None:
        counters = (
            self.accepted_datagrams,
            self.duplicate_datagrams,
            self.out_of_order_datagrams,
            self.estimated_lost_datagrams,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("sequence counters must be non-negative integers")
        for value in (self.first_sequence, self.last_sequence):
            if value is not None and (type(value) is not int or not 0 <= value < 2**32):
                raise ValueError("sequence endpoints must be uint32 or null")
        if self.accepted_datagrams == 0 and (
            self.first_sequence is not None or self.last_sequence is not None
        ):
            raise ValueError("empty sequence stats cannot contain endpoints")
        if self.accepted_datagrams > 0 and (
            self.first_sequence is None or self.last_sequence is None
        ):
            raise ValueError("non-empty sequence stats require endpoints")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "accepted_datagrams": self.accepted_datagrams,
            "duplicate_datagrams": self.duplicate_datagrams,
            "out_of_order_datagrams": self.out_of_order_datagrams,
            "estimated_lost_datagrams": self.estimated_lost_datagrams,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SequenceStats:
        def endpoint(name: str) -> int | None:
            value = data.get(name)
            return None if value is None else _strict_int(value, name)

        return cls(
            accepted_datagrams=_strict_int(
                data["accepted_datagrams"], "accepted_datagrams"
            ),
            duplicate_datagrams=_strict_int(
                data["duplicate_datagrams"], "duplicate_datagrams"
            ),
            out_of_order_datagrams=_strict_int(
                data["out_of_order_datagrams"], "out_of_order_datagrams"
            ),
            estimated_lost_datagrams=_strict_int(
                data["estimated_lost_datagrams"], "estimated_lost_datagrams"
            ),
            first_sequence=endpoint("first_sequence"),
            last_sequence=endpoint("last_sequence"),
        )


@dataclass(frozen=True)
class SessionManifest:
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
    capture_file: str
    capture_sha256: str
    start_host_timestamp_ns: int
    end_host_timestamp_ns: int
    radio_config: RadioToneConfig
    sender_allowlist: tuple[str, ...]
    transmitter_allowlist: tuple[str, ...]
    sequence_stats: SequenceStats = field(default_factory=SequenceStats)
    registration_payload: str = "register-v2"
    framing: str = "u32-be-length+csi2-datagram"
    synthetic: bool = False
    privacy_notice: str = (
        "CSI includes transmitter identifiers and occupancy-correlated radio data; "
        "collect only with authorization and do not publish raw captures."
    )
    schema_version: int = 1
    _verified_record_fingerprints: tuple[tuple[object, ...], ...] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            type(self.start_host_timestamp_ns) is not int
            or type(self.end_host_timestamp_ns) is not int
        ):
            raise ValueError("host timestamps must be integers")
        if not isinstance(self.radio_config, RadioToneConfig) or not isinstance(
            self.sequence_stats, SequenceStats
        ):
            raise TypeError("manifest radio_config/sequence_stats have wrong types")
        required = {
            "session_id": self.session_id,
            "receiver_id": self.receiver_id,
            "router_model": self.router_model,
            "interface": self.interface,
            "boot_id": self.boot_id,
            "radio_epoch": self.radio_epoch,
            "timebase_id": self.timebase_id,
            "driver_commit": self.driver_commit,
            "source_tree_hash": self.source_tree_hash,
            "capture_file": self.capture_file,
            "capture_sha256": self.capture_sha256,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"session manifest missing fields: {', '.join(missing)}")
        if self.end_host_timestamp_ns < self.start_host_timestamp_ns:
            raise ValueError("session end precedes session start")
        if type(self.clock_uncertainty_ns) is not int or self.clock_uncertainty_ns < 0:
            raise ValueError("clock_uncertainty_ns cannot be negative")
        if type(self.synthetic) is not bool:
            raise ValueError("synthetic must be boolean")
        if self.start_host_timestamp_ns < 0:
            raise ValueError("host timestamps cannot be negative")
        if self.registration_payload != "register-v2":
            raise ValueError("only the Stage-2 register-v2 handshake is supported")
        if self.framing != "u32-be-length+csi2-datagram":
            raise ValueError("unknown capture framing")
        if len(self.capture_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.capture_sha256
        ):
            raise ValueError("capture_sha256 must be lowercase SHA-256 hex")
        if len(self.source_tree_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_tree_hash
        ):
            raise ValueError("source_tree_hash must be lowercase SHA-256 hex")
        if Path(self.capture_file).name != self.capture_file or self.capture_file in {
            ".",
            "..",
        }:
            raise ValueError("capture_file must be a safe basename")
        if not self.capture_file.endswith(".csi2f"):
            raise ValueError("session captures must use the .csi2f framing suffix")
        if not self.synthetic and (
            not 7 <= len(self.driver_commit) <= 64
            or any(
                character not in "0123456789abcdef" for character in self.driver_commit
            )
        ):
            raise ValueError("driver_commit must be a hexadecimal revision")
        if not self.sender_allowlist or any(
            not value.strip() for value in self.sender_allowlist
        ):
            raise ValueError("sender_allowlist must be non-empty")
        for endpoint in self.sender_allowlist:
            try:
                host, port_text = endpoint.rsplit(":", 1)
                ipaddress.ip_address(host)
                port = int(port_text)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "sender_allowlist entries must be IP:source-port"
                ) from exc
            if not 1 <= port <= 65_535:
                raise ValueError("sender source port is outside 1..65535")
        if len(self.transmitter_allowlist) != 1:
            raise ValueError("exactly one transmitter address is required per session")
        normalized_tas = tuple(
            normalize_mac(value) for value in self.transmitter_allowlist
        )
        object.__setattr__(self, "transmitter_allowlist", normalized_tas)

    @property
    def time_window_ns(self) -> tuple[int, int]:
        return self.start_host_timestamp_ns, self.end_host_timestamp_ns

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "ax3000t-csi-session",
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "receiver_id": self.receiver_id,
            "router_model": self.router_model,
            "interface": self.interface,
            "boot_id": self.boot_id,
            "radio_epoch": self.radio_epoch,
            "timebase": {
                "id": self.timebase_id,
                "maximum_uncertainty_ns": self.clock_uncertainty_ns,
            },
            "driver_commit": self.driver_commit,
            "source_tree_hash": self.source_tree_hash,
            "capture": {
                "file": self.capture_file,
                "sha256": self.capture_sha256,
                "framing": self.framing,
                "start_host_timestamp_ns": self.start_host_timestamp_ns,
                "end_host_timestamp_ns": self.end_host_timestamp_ns,
            },
            "udp": {
                "registration_payload": self.registration_payload,
                "sender_allowlist": list(self.sender_allowlist),
                "sequence_stats": self.sequence_stats.to_dict(),
            },
            "transmitter_allowlist": list(self.transmitter_allowlist),
            "radio_config": self.radio_config.to_dict(),
            "synthetic": self.synthetic,
            "privacy_notice": self.privacy_notice,
        }

    def computed_artifact_id(self) -> str:
        encoded = json.dumps(
            json_safe(self.to_dict()),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionManifest:
        if (
            data.get("schema") != "ax3000t-csi-session"
            or _strict_int(data.get("schema_version"), "schema_version") != 1
        ):
            raise ValueError("unsupported session-manifest schema")
        capture = data["capture"]
        udp = data["udp"]
        timebase = data["timebase"]
        return cls(
            session_id=_strict_str(data["session_id"], "session_id"),
            receiver_id=_strict_str(data["receiver_id"], "receiver_id"),
            router_model=_strict_str(data["router_model"], "router_model"),
            interface=_strict_str(data["interface"], "interface"),
            boot_id=_strict_str(data["boot_id"], "boot_id"),
            radio_epoch=_strict_str(data["radio_epoch"], "radio_epoch"),
            timebase_id=_strict_str(timebase["id"], "timebase.id"),
            clock_uncertainty_ns=_strict_int(
                timebase["maximum_uncertainty_ns"], "maximum_uncertainty_ns"
            ),
            driver_commit=_strict_str(data["driver_commit"], "driver_commit"),
            source_tree_hash=_strict_str(data["source_tree_hash"], "source_tree_hash"),
            capture_file=_strict_str(capture["file"], "capture.file"),
            capture_sha256=_strict_str(capture["sha256"], "capture.sha256"),
            start_host_timestamp_ns=_strict_int(
                capture["start_host_timestamp_ns"], "start_host_timestamp_ns"
            ),
            end_host_timestamp_ns=_strict_int(
                capture["end_host_timestamp_ns"], "end_host_timestamp_ns"
            ),
            radio_config=RadioToneConfig.from_dict(data["radio_config"]),
            sender_allowlist=tuple(
                _strict_str(value, "sender_allowlist[]")
                for value in udp["sender_allowlist"]
            ),
            transmitter_allowlist=tuple(
                _strict_str(value, "transmitter_allowlist[]")
                for value in data["transmitter_allowlist"]
            ),
            sequence_stats=SequenceStats.from_dict(udp["sequence_stats"]),
            registration_payload=_strict_str(
                udp["registration_payload"], "registration_payload"
            ),
            framing=_strict_str(capture["framing"], "capture.framing"),
            synthetic=data.get("synthetic", False),
            privacy_notice=_strict_str(
                data.get("privacy_notice", ""), "privacy_notice"
            ),
            schema_version=_strict_int(data["schema_version"], "schema_version"),
        )

    def save(self, path: str | Path) -> None:
        dump_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> SessionManifest:
        return cls.from_dict(load_json(path))

    def assert_radio_epoch_compatible(self, other: SessionManifest) -> None:
        fields = (
            "receiver_id",
            "boot_id",
            "radio_epoch",
            "driver_commit",
            "source_tree_hash",
        )
        mismatches = [
            field_name
            for field_name in fields
            if getattr(self, field_name) != getattr(other, field_name)
        ]
        if self.radio_config.signature() != other.radio_config.signature():
            mismatches.append("radio_config")
        if mismatches:
            raise ValueError(
                "capture is incompatible with calibration provenance: "
                + ", ".join(mismatches)
            )

    def assert_records_match(self, records: list[CSIRecord]) -> None:
        if not records:
            raise ValueError("session contains no records")
        configs = {validate_analysis_record(record).signature() for record in records}
        if configs != {self.radio_config.signature()}:
            raise ValueError("records do not match the manifest radio/tone config")
        tas = {record.transmitter_address for record in records}
        if len(tas) != 1:
            raise ValueError("one inference window must contain exactly one TA")
        if self.transmitter_allowlist and not tas <= set(self.transmitter_allowlist):
            raise ValueError("record TA is outside the manifest allowlist")
        start = min(record.host_timestamp_ns for record in records)
        end = max(record.host_timestamp_ns for record in records)
        if start < self.start_host_timestamp_ns or end > self.end_host_timestamp_ns:
            raise ValueError("record timestamps fall outside the manifest window")

    def assert_records_verified(
        self,
        records: list[CSIRecord],
        *,
        capture_path: str | Path | None = None,
    ) -> None:
        """Bind supplied records to capture bytes verified by this manifest.

        Real-data callers must either provide ``capture_path`` here or call
        :meth:`verify_capture` on this exact manifest instance first.  The
        private proof is deliberately not serialized, so loading a manifest
        cannot manufacture a successful byte-verification state.  Synthetic
        demo manifests retain their explicit, isolated in-memory path.
        """

        if capture_path is not None:
            self.verify_capture(capture_path)
        self.assert_records_match(records)
        if self.synthetic:
            return
        verified = self._verified_record_fingerprints
        if verified is None:
            raise ValueError(
                "real capture bytes were not verified against this manifest"
            )
        available = Counter(verified)
        requested = Counter(_record_fingerprint(record) for record in records)
        if requested - available:
            raise ValueError(
                "records are not an exact subset of the manifest-verified capture"
            )

    def verify_capture(self, path: str | Path) -> list[CSIRecord]:
        """Verify filename, SHA, framing, records, and sequence endpoints."""

        # A failed re-verification must revoke any proof obtained earlier.
        object.__setattr__(self, "_verified_record_fingerprints", None)
        capture = Path(path)
        if capture.name != self.capture_file:
            raise ValueError("capture filename differs from the manifest")
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError("capture verification requires O_NOFOLLOW support")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(capture, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("capture must be a regular file")
            digest = hashlib.sha256()
            payload = bytearray()
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    payload.extend(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or len(payload) != before.st_size:
            raise ValueError("capture changed while it was being verified")
        if digest.hexdigest() != self.capture_sha256:
            raise ValueError("capture SHA-256 differs from the manifest")
        from .csi2 import iter_length_prefixed_csi2

        records = list(iter_length_prefixed_csi2(payload))
        self.assert_records_match(records)
        if len(records) != self.sequence_stats.accepted_datagrams:
            raise ValueError("framed record count differs from sequence stats")
        if records[0].sequence != self.sequence_stats.first_sequence:
            raise ValueError("first sequence differs from manifest stats")
        if records[-1].sequence != self.sequence_stats.last_sequence:
            raise ValueError("last sequence differs from manifest stats")
        object.__setattr__(
            self,
            "_verified_record_fingerprints",
            tuple(_record_fingerprint(record) for record in records),
        )
        return records


def _record_fingerprint(record: CSIRecord) -> tuple[object, ...]:
    """Exact semantic identity for one already-decoded CSI2 record."""

    return (
        record.sequence,
        record.host_timestamp_ns,
        record.driver_timestamp,
        record.transmitter_address,
        record.band,
        record.channel_frequency_mhz,
        record.channel_bandwidth,
        record.data_bandwidth,
        record.primary_channel_index,
        record.rx_idx,
        record.tx_idx,
        record.quality_flags,
        record.presence_flags,
        record.packet_sequence_number,
        record.segment_number,
        record.remain_last,
        record.transport_stream,
        record.h_idx,
        record.chain_info,
        record.rssi_raw,
        record.snr_raw,
        record.rx_mode,
        record.rate_mcs,
        record.rate_nss,
        record.rate_guard_interval,
        record.rate_kbps,
        record.ext_info,
        record.protocol_version,
        record.samples.dtype.str,
        record.samples.shape,
        record.samples.tobytes(order="C"),
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def windows_overlap(
    left: tuple[int, int], right: tuple[int, int], *, minimum_overlap_ns: int = 1
) -> bool:
    return min(left[1], right[1]) - max(left[0], right[0]) >= minimum_overlap_ns
