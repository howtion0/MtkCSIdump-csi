"""Evidence-gated same-PPDU grouping without stream/chain collapse."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .contracts import AnalysisContractError, validate_analysis_record
from .csi2 import CSI_PRESENT_BAND, CSI_PRESENT_PKT_SN, quality_flag_names
from .models import CSIRecord, Evidence


@dataclass(frozen=True, order=True)
class StreamKey:
    """One firmware CSI stream; two RX values may share Tx/transport stream."""

    tx_idx: int
    rx_idx: int
    transport_stream: int | None


HARD_FLAG_PREFIXES = (
    "analysis_contract:",
    "cross_stream_metadata_conflict:",
)
HARD_FLAGS = {
    "conflicting_duplicate_stream",
    "chain_length_mismatch",
    "driver_reported_incomplete_segment",
}


def _is_hard_flag(flag: str) -> bool:
    return flag in HARD_FLAGS or flag.startswith(HARD_FLAG_PREFIXES)


@dataclass
class GroupedPPDU:
    """Records believed to describe one PPDU, indexed by full stream identity."""

    records_by_stream: dict[StreamKey, CSIRecord]
    evidence: Evidence
    identity_mode: str
    identity: tuple[object, ...]

    @property
    def stream_keys(self) -> tuple[StreamKey, ...]:
        return tuple(sorted(self.records_by_stream))

    @property
    def chain_ids(self) -> tuple[int, ...]:
        return tuple(sorted({key.rx_idx for key in self.records_by_stream}))

    @property
    def host_timestamp_ns(self) -> int:
        return min(
            record.host_timestamp_ns for record in self.records_by_stream.values()
        )

    @property
    def hard_failure_flags(self) -> tuple[str, ...]:
        return tuple(flag for flag in self.evidence.flags if _is_hard_flag(flag))

    @property
    def phase_usable(self) -> bool:
        return (
            len(self.chain_ids) >= 2
            and self.identity_mode == "strict"
            and self.evidence.score >= 0.85
            and not self.hard_failure_flags
        )

    def pair(
        self,
        reference_rx_idx: int,
        target_rx_idx: int,
        *,
        tx_idx: int | None = None,
        transport_stream: int | None = None,
        require_transport_stream_absent: bool = False,
        allow_low_confidence_identity: bool = False,
    ) -> tuple[CSIRecord, CSIRecord]:
        """Select two RX observations from exactly one Tx/stream context.

        ``allow_low_confidence_identity`` only relaxes missing strict identity.
        It never bypasses tone order, truncation, segment, length, metadata, or
        duplicate-conflict gates.
        """

        if reference_rx_idx == target_rx_idx:
            raise ValueError("reference and target RX indices must differ")
        if transport_stream is not None and require_transport_stream_absent:
            raise ValueError("transport-stream selectors are mutually exclusive")
        if self.hard_failure_flags:
            raise ValueError(
                "inter-chain phase refused by hard evidence gates: "
                + ", ".join(self.hard_failure_flags)
            )
        if not allow_low_confidence_identity and not self.phase_usable:
            raise ValueError(
                "inter-chain phase refused: same-PPDU identity is not strongly proven"
            )

        contexts = {
            (key.tx_idx, key.transport_stream)
            for key in self.records_by_stream
            if key.rx_idx in {reference_rx_idx, target_rx_idx}
            and (tx_idx is None or key.tx_idx == tx_idx)
            and (transport_stream is None or key.transport_stream == transport_stream)
            and (not require_transport_stream_absent or key.transport_stream is None)
        }
        candidates: list[tuple[CSIRecord, CSIRecord]] = []
        for candidate_tx, candidate_stream in sorted(
            contexts, key=lambda item: (item[0], -1 if item[1] is None else item[1])
        ):
            left_key = StreamKey(candidate_tx, reference_rx_idx, candidate_stream)
            right_key = StreamKey(candidate_tx, target_rx_idx, candidate_stream)
            if (
                left_key in self.records_by_stream
                and right_key in self.records_by_stream
            ):
                candidates.append(
                    (
                        self.records_by_stream[left_key],
                        self.records_by_stream[right_key],
                    )
                )
        if not candidates:
            raise KeyError("requested RX pair is absent in one common Tx/stream")
        if len(candidates) != 1:
            raise ValueError(
                "ambiguous Tx/transport stream; ChainMapping must select both"
            )
        left, right = candidates[0]
        if left.sample_count != right.sample_count:
            raise ValueError("receive-chain CSI lengths differ")
        left_config = validate_analysis_record(left)
        right_config = validate_analysis_record(right)
        if left_config.signature() != right_config.signature():
            raise ValueError("selected receive chains have incompatible radio metadata")
        return left, right


def _stream_key(record: CSIRecord) -> StreamKey:
    return StreamKey(record.tx_idx, record.rx_idx, record.transport_stream)


def _strict_key(record: CSIRecord) -> tuple[object, ...] | None:
    has_packet_number = record.has(CSI_PRESENT_PKT_SN, record.packet_sequence_number)
    has_band = bool(record.presence_flags & CSI_PRESENT_BAND)
    has_driver_clock = record.driver_timestamp != 0
    if not (has_packet_number and has_band and has_driver_clock):
        return None
    return (
        record.transmitter_address,
        record.band,
        int(record.packet_sequence_number),
        record.driver_timestamp,
    )


def _record_fingerprint(record: CSIRecord) -> tuple[object, ...]:
    """Material fields; host arrival sequence/time are not radio evidence."""

    return (
        record.driver_timestamp,
        record.transmitter_address,
        record.band,
        record.channel_frequency_mhz,
        record.channel_bandwidth,
        record.data_bandwidth,
        record.tx_idx,
        record.rx_idx,
        record.transport_stream,
        record.presence_flags,
        record.quality_flags,
        record.packet_sequence_number,
        record.segment_number,
        record.remain_last,
        record.h_idx,
        record.chain_info,
        record.primary_channel_index,
        record.rx_mode,
        record.rate_mcs,
        record.rate_nss,
        record.sample_count,
        record.samples.tobytes(),
    )


def _merge_stream_records(
    records: list[CSIRecord],
) -> tuple[CSIRecord, tuple[str, ...]]:
    if len(records) == 1:
        return records[0], ()
    fingerprints = {_record_fingerprint(record) for record in records}
    if len(fingerprints) == 1:
        return min(records, key=lambda record: record.sequence), (
            "equivalent_duplicate_stream_deduplicated",
        )
    return min(records, key=lambda record: record.sequence), (
        "conflicting_duplicate_stream",
    )


def _metadata_flags(records: dict[StreamKey, CSIRecord]) -> tuple[str, ...]:
    flags: list[str] = []
    for record in records.values():
        try:
            validate_analysis_record(record)
        except AnalysisContractError as exc:
            flags.append(f"analysis_contract:{exc}")

    by_context: dict[tuple[int, int | None], list[CSIRecord]] = defaultdict(list)
    for key, record in records.items():
        by_context[(key.tx_idx, key.transport_stream)].append(record)
    fields = (
        "channel_frequency_mhz",
        "channel_bandwidth",
        "data_bandwidth",
        "sample_count",
        "primary_channel_index",
        "rx_mode",
        "rate_mcs",
        "rate_nss",
        "segment_number",
        "remain_last",
        "quality_flags",
        "presence_flags",
    )
    for context_records in by_context.values():
        if len(context_records) < 2:
            continue
        for field_name in fields:
            if len({getattr(record, field_name) for record in context_records}) > 1:
                flags.append(f"cross_stream_metadata_conflict:{field_name}")
    return tuple(flags)


def _make_group(
    records: list[CSIRecord],
    identity_mode: str,
    identity: tuple[object, ...],
    base_score: float,
    base_flags: tuple[str, ...] = (),
) -> GroupedPPDU:
    buckets: dict[StreamKey, list[CSIRecord]] = defaultdict(list)
    for record in records:
        buckets[_stream_key(record)].append(record)

    merged: dict[StreamKey, CSIRecord] = {}
    flags = list(base_flags)
    for key, stream_records in buckets.items():
        merged[key], stream_flags = _merge_stream_records(stream_records)
        flags.extend(stream_flags)
    flags.extend(_metadata_flags(merged))

    if len({key.rx_idx for key in merged}) < 2:
        flags.append("single_chain_only")
    if len({record.sample_count for record in merged.values()}) > 1:
        flags.append("chain_length_mismatch")
    if any(record.remain_last != 0 for record in merged.values()):
        flags.append("driver_reported_incomplete_segment")

    hard_count = sum(_is_hard_flag(flag) for flag in flags)
    penalty = min(0.85, 0.35 * hard_count)
    if "single_chain_only" in flags:
        penalty += 0.10
    score = float(np.clip(base_score - penalty, 0.0, 1.0))
    unique_flags = tuple(dict.fromkeys(flags))
    quality_union = 0
    presence_intersection = (1 << 16) - 1
    for record in merged.values():
        quality_union |= record.quality_flags
        presence_intersection &= record.presence_flags
    return GroupedPPDU(
        records_by_stream=merged,
        evidence=Evidence(
            score=score,
            flags=unique_flags,
            details={
                "identity_mode": identity_mode,
                "rx_chain_count": len({key.rx_idx for key in merged}),
                "stream_count": len(merged),
                "record_count": len(records),
                "quality_flags_union": quality_union,
                "quality_flag_names": list(quality_flag_names(quality_union)),
                "presence_flags_intersection": presence_intersection,
                "hard_gate_passed": not any(_is_hard_flag(flag) for flag in flags),
            },
        ),
        identity_mode=identity_mode,
        identity=identity,
    )


def _fallback_compatible(
    anchor: CSIRecord, candidate: CSIRecord, window_ns: int
) -> bool:
    if _stream_key(anchor) == _stream_key(candidate):
        return False
    if anchor.transmitter_address != candidate.transmitter_address:
        return False
    if anchor.band != candidate.band:
        return False
    if anchor.channel_frequency_mhz != candidate.channel_frequency_mhz:
        return False
    if anchor.channel_bandwidth != candidate.channel_bandwidth:
        return False
    if anchor.data_bandwidth != candidate.data_bandwidth:
        return False
    if anchor.sample_count != candidate.sample_count:
        return False
    if abs(anchor.host_timestamp_ns - candidate.host_timestamp_ns) > window_ns:
        return False
    for left, right in (
        (anchor.packet_sequence_number, candidate.packet_sequence_number),
        (anchor.driver_timestamp or None, candidate.driver_timestamp or None),
    ):
        if left is not None and right is not None and left != right:
            return False
    return True


def _fallback_score(
    records: list[CSIRecord], window_ns: int
) -> tuple[float, tuple[str, ...]]:
    flags = ["fallback_grouping", "not_proven_same_ppdu"]
    score = 0.22
    packet_numbers = {
        record.packet_sequence_number
        for record in records
        if record.packet_sequence_number is not None
    }
    driver_times = {
        record.driver_timestamp for record in records if record.driver_timestamp != 0
    }
    if len(packet_numbers) == 1 and all(
        record.packet_sequence_number is not None for record in records
    ):
        score += 0.22
    else:
        flags.append("packet_sequence_missing")
    if len(driver_times) == 1 and all(
        record.driver_timestamp != 0 for record in records
    ):
        score += 0.24
    else:
        flags.append("driver_timestamp_missing")
    spread = max(record.host_timestamp_ns for record in records) - min(
        record.host_timestamp_ns for record in records
    )
    score += 0.12 * max(0.0, 1.0 - spread / max(window_ns, 1))
    if all(record.presence_flags & CSI_PRESENT_BAND for record in records):
        score += 0.08
    else:
        flags.append("band_presence_missing")
    return min(score, 0.79), tuple(flags)


def group_same_ppdu(
    records: Iterable[CSIRecord],
    *,
    fallback_window_ns: int = 250_000,
    strict_identity_window_ns: int = 1_000_000,
) -> list[GroupedPPDU]:
    """Group by TA+band+pkt_sn+driver_ts and split wrap/reuse epochs.

    The bounded host-time epoch prevents a repeated 16/32-bit packet identity
    after wrap from colliding with an old PPDU.  Stage-2 has already reassembled
    80 MHz: ``segment_number`` is provenance and is never concatenated here.
    """

    if fallback_window_ns <= 0 or strict_identity_window_ns <= 0:
        raise ValueError("grouping windows must be positive")
    ordered = sorted(
        records, key=lambda record: (record.host_timestamp_ns, record.sequence)
    )
    strict_buckets: dict[tuple[object, ...], list[list[CSIRecord]]] = defaultdict(list)
    weak: list[CSIRecord] = []
    for record in ordered:
        key = _strict_key(record)
        if key is None:
            weak.append(record)
            continue
        epochs = strict_buckets[key]
        if not epochs or (
            record.host_timestamp_ns - epochs[-1][-1].host_timestamp_ns
            > strict_identity_window_ns
        ):
            epochs.append([record])
        else:
            epochs[-1].append(record)

    groups: list[GroupedPPDU] = []
    for key, epochs in strict_buckets.items():
        for epoch_index, bucket in enumerate(epochs):
            flags = ("reused_identity_after_host_gap",) if epoch_index > 0 else ()
            groups.append(
                _make_group(bucket, "strict", (*key, epoch_index), 0.98, flags)
            )

    fallback_buckets: list[list[CSIRecord]] = []
    for record in weak:
        candidates: list[tuple[int, int]] = []
        for index, bucket in enumerate(fallback_buckets):
            if any(_stream_key(item) == _stream_key(record) for item in bucket):
                continue
            if _fallback_compatible(bucket[0], record, fallback_window_ns):
                candidates.append(
                    (abs(bucket[0].host_timestamp_ns - record.host_timestamp_ns), index)
                )
        if candidates:
            fallback_buckets[min(candidates)[1]].append(record)
        else:
            fallback_buckets.append([record])

    for index, bucket in enumerate(fallback_buckets):
        score, flags = _fallback_score(bucket, fallback_window_ns)
        identity = (
            "fallback",
            bucket[0].transmitter_address,
            bucket[0].band,
            min(record.host_timestamp_ns for record in bucket),
            index,
        )
        groups.append(_make_group(bucket, "fallback", identity, score, flags))
    return sorted(groups, key=lambda group: group.host_timestamp_ns)
