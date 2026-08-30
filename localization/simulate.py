"""Deterministic synthetic fixtures; never hardware evidence."""

from __future__ import annotations

import hashlib

import numpy as np

from .calibration import (
    AntennaElement,
    ChainMapping,
    expected_pair_ratio,
    subcarrier_frequencies_hz,
)
from .contracts import validate_analysis_record
from .csi2 import (
    CSI_PRESENT_BAND,
    CSI_PRESENT_CHAIN_INFO,
    CSI_PRESENT_CHANNEL_FREQ,
    CSI_PRESENT_H_IDX,
    CSI_PRESENT_PKT_SN,
    CSI_PRESENT_REMAIN_LAST,
    CSI_PRESENT_RX_MODE,
    CSI_PRESENT_SEGMENT_NUM,
    CSI_PRESENT_TR_STREAM,
    CSI_QUALITY_TONE_MASKED_REORDERED,
)
from .grouping import GroupedPPDU, group_same_ppdu
from .models import CSIRecord
from .session import SequenceStats, SessionManifest

SYNTHETIC_NOTICE = "SYNTHETIC SIMULATION — NOT HARDWARE EVIDENCE"


def default_mapping(
    *,
    spacing_m: float = 0.028,
    receiver_id: str = "synthetic-ax3000t",
    broadside_heading_deg: float = 0.0,
) -> ChainMapping:
    """Two elements placed along y, hence broadside points along +x."""

    return ChainMapping(
        receiver_id=receiver_id,
        reference_rx_idx=0,
        target_rx_idx=1,
        tx_idx=0,
        transport_stream=0,
        elements=(
            AntennaElement(0, "synthetic-reference", (0.0, -spacing_m / 2), "SYN0"),
            AntennaElement(1, "synthetic-target", (0.0, spacing_m / 2), "SYN1"),
        ),
        array_broadside_heading_deg=broadside_heading_deg,
        notes=SYNTHETIC_NOTICE,
    )


def synthetic_hardware_ratio(sample_count: int) -> np.ndarray:
    tone = np.linspace(-1.0, 1.0, sample_count)
    amplitude = 0.84 + 0.06 * np.cos(np.pi * tone)
    phase = 0.72 + 0.30 * tone + 0.08 * tone**2
    return amplitude * np.exp(1j * phase)


def simulate_strict_groups(
    *,
    angle_deg: float,
    packet_count: int = 32,
    sample_count: int = 64,
    channel_frequency_mhz: int = 5500,
    channel_bandwidth_enum: int = 0,
    data_bandwidth_enum: int = 0,
    mapping: ChainMapping | None = None,
    hardware_ratio: np.ndarray | None = None,
    noise_std: float = 0.025,
    seed: int = 7,
    transmitter_address: str = "02:00:00:00:00:01",
    distance_m: float = 2.5,
) -> list[GroupedPPDU]:
    mapping = mapping or default_mapping()
    hardware_ratio = (
        synthetic_hardware_ratio(sample_count)
        if hardware_ratio is None
        else np.asarray(hardware_ratio, dtype=np.complex128)
    )
    if hardware_ratio.shape != (sample_count,):
        raise ValueError("hardware_ratio shape does not match sample_count")
    rng = np.random.default_rng(seed)
    frequencies = subcarrier_frequencies_hz(sample_count, channel_frequency_mhz)
    spatial = expected_pair_ratio(angle_deg, mapping, frequencies)
    presence = (
        CSI_PRESENT_H_IDX
        | CSI_PRESENT_CHAIN_INFO
        | CSI_PRESENT_PKT_SN
        | CSI_PRESENT_SEGMENT_NUM
        | CSI_PRESENT_REMAIN_LAST
        | CSI_PRESENT_TR_STREAM
        | CSI_PRESENT_RX_MODE
        | CSI_PRESENT_CHANNEL_FREQ
        | CSI_PRESENT_BAND
    )
    records: list[CSIRecord] = []
    base_rssi = round(-38.0 - 9.0 * np.log2(max(distance_m, 0.25)))
    tone = np.arange(sample_count, dtype=float)
    for packet in range(packet_count):
        # Shared frequency-selective channel plus a small echo. It cancels in
        # the ideal inter-chain ratio, as simultaneous MIMO observations should.
        packet_phase = rng.uniform(-np.pi, np.pi)
        shared = np.exp(1j * packet_phase) * (
            1.0 + 0.28 * np.exp(-1j * 2.0 * np.pi * tone * 5.0 / sample_count)
        )
        reference = 1500.0 * shared
        target = reference * hardware_ratio * spatial
        reference += (
            noise_std
            * 1500.0
            * (rng.normal(size=sample_count) + 1j * rng.normal(size=sample_count))
        )
        target += (
            noise_std
            * 1500.0
            * (rng.normal(size=sample_count) + 1j * rng.normal(size=sample_count))
        )
        host = 1_800_000_000_000_000_000 + packet * 5_000_000
        driver = 10_000 + packet * 50
        common = {
            "host_timestamp_ns": host,
            "driver_timestamp": driver,
            "transmitter_address": transmitter_address,
            "band": 1,
            "channel_frequency_mhz": channel_frequency_mhz,
            "channel_bandwidth": channel_bandwidth_enum,
            "tx_idx": 0,
            "presence_flags": presence,
            "packet_sequence_number": packet % 4096,
            "segment_number": 0,
            "remain_last": 0,
            "transport_stream": 0,
            "h_idx": packet,
            "chain_info": 0b11,
            "rssi_raw": base_rssi + int(rng.integers(-1, 2)),
            "snr_raw": 31 + int(rng.integers(-1, 2)),
            "quality_flags": CSI_QUALITY_TONE_MASKED_REORDERED,
            "data_bandwidth": data_bandwidth_enum,
            "primary_channel_index": 0,
            # Exact mt76 enum handled by the audited Stage-2 type-5 VHT path.
            "rx_mode": 4,
        }
        records.append(
            CSIRecord(
                sequence=2 * packet,
                rx_idx=mapping.reference_rx_idx,
                samples=reference,
                **common,
            )
        )
        records.append(
            CSIRecord(
                sequence=2 * packet + 1,
                rx_idx=mapping.target_rx_idx,
                samples=target,
                **common,
            )
        )
    return group_same_ppdu(records)


def synthetic_session_manifest(
    groups: list[GroupedPPDU],
    *,
    receiver_id: str,
    session_id: str,
    boot_id: str | None = None,
    radio_epoch: str = "synthetic-radio-epoch-1",
    timebase_id: str = "synthetic-shared-timebase",
    clock_uncertainty_ns: int = 1_000,
) -> SessionManifest:
    records = [
        record for group in groups for record in group.records_by_stream.values()
    ]
    if not records:
        raise ValueError("synthetic manifest requires records")
    configs = {validate_analysis_record(record).signature() for record in records}
    if len(configs) != 1:
        raise ValueError("synthetic records contain multiple radio configs")
    radio_config = validate_analysis_record(records[0])
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return SessionManifest(
        session_id=session_id,
        receiver_id=receiver_id,
        router_model="SYNTHETIC AX3000T",
        interface="synthetic-radio0",
        boot_id=boot_id or f"{receiver_id}-boot-1",
        radio_epoch=radio_epoch,
        timebase_id=timebase_id,
        clock_uncertainty_ns=clock_uncertainty_ns,
        driver_commit="synthetic-stage2-commit",
        source_tree_hash="0" * 64,
        capture_file=f"{session_id}.synthetic.csi2f",
        capture_sha256=digest,
        start_host_timestamp_ns=min(record.host_timestamp_ns for record in records),
        end_host_timestamp_ns=max(record.host_timestamp_ns for record in records),
        radio_config=radio_config,
        sender_allowlist=("127.0.0.1:8888",),
        transmitter_allowlist=(records[0].transmitter_address,),
        sequence_stats=SequenceStats(
            accepted_datagrams=len(records),
            first_sequence=min(record.sequence for record in records),
            last_sequence=max(record.sequence for record in records),
        ),
        synthetic=True,
    )


def synthetic_range_dataset(
    *,
    seed: int = 17,
    samples_per_class: int = 30,
    device_shift: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return four features for near/mid/far; optional shift mimics new hardware."""

    rng = np.random.default_rng(seed)
    centers = {
        "near": np.asarray([-43.0, 6.20, 0.28, 32.0]),
        "mid": np.asarray([-56.0, 5.25, 0.42, 23.0]),
        "far": np.asarray([-70.0, 4.35, 0.59, 14.0]),
    }
    noise = np.asarray([2.0, 0.18, 0.035, 1.8])
    shift = (
        np.zeros(4) if device_shift is None else np.asarray(device_shift, dtype=float)
    )
    rows: list[np.ndarray] = []
    labels: list[str] = []
    for label, center in centers.items():
        samples = center + shift + rng.normal(size=(samples_per_class, 4)) * noise
        rows.extend(samples)
        labels.extend([label] * samples_per_class)
    return np.vstack(rows), np.asarray(labels)
