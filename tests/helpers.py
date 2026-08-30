from __future__ import annotations

import struct

import numpy as np

from localization.csi2 import (
    CSI_PRESENT_BAND,
    CSI_PRESENT_CHAIN_INFO,
    CSI_PRESENT_CHANNEL_FREQ,
    CSI_PRESENT_H_IDX,
    CSI_PRESENT_PKT_SN,
    CSI_PRESENT_RATE_KBPS,
    CSI_PRESENT_RATE_MCS,
    CSI_PRESENT_RATE_NSS,
    CSI_PRESENT_REMAIN_LAST,
    CSI_PRESENT_RX_MODE,
    CSI_PRESENT_SEGMENT_NUM,
    CSI_PRESENT_TR_STREAM,
    CSI_QUALITY_TONE_MASKED_REORDERED,
    EXPECTED_SAMPLE_COUNT_BY_DATA_BW,
    V2_HEADER_FORMAT,
    V2_HEADER_SIZE,
)
from localization.grouping import group_same_ppdu
from localization.models import CSIRecord
from localization.session import SessionManifest
from localization.simulate import synthetic_session_manifest

ALL_PRESENCE = (
    CSI_PRESENT_H_IDX
    | CSI_PRESENT_CHAIN_INFO
    | CSI_PRESENT_PKT_SN
    | CSI_PRESENT_SEGMENT_NUM
    | CSI_PRESENT_REMAIN_LAST
    | CSI_PRESENT_TR_STREAM
    | CSI_PRESENT_RX_MODE
    | CSI_PRESENT_RATE_MCS
    | CSI_PRESENT_RATE_NSS
    | CSI_PRESENT_RATE_KBPS
    | CSI_PRESENT_CHANNEL_FREQ
    | CSI_PRESENT_BAND
)


def make_record(
    *,
    rx_idx: int,
    packet_number: int = 9,
    driver_timestamp: int = 1234,
    host_timestamp_ns: int = 1_000_000_000,
    sequence: int = 1,
    presence_flags: int = ALL_PRESENCE,
    quality_flags: int = CSI_QUALITY_TONE_MASKED_REORDERED,
    samples: np.ndarray | None = None,
    frequency_mhz: int = 5500,
    channel_bw_enum: int = 0,
    data_bw_enum: int = 0,
    tx_idx: int = 0,
    transport_stream: int = 0,
    segment_number: int = 0,
    remain_last: int = 0,
    rx_mode: int = 4,
    primary_channel_index: int = 0,
    protocol_version: int = 2,
    transmitter_address: str = "02:11:22:33:44:55",
) -> CSIRecord:
    count = EXPECTED_SAMPLE_COUNT_BY_DATA_BW[data_bw_enum]
    values = (
        np.asarray(
            np.arange(count, dtype=float) + 100.0 + 1j * (20.0 - np.arange(count)),
            dtype=np.complex128,
        )
        if samples is None
        else np.asarray(samples, dtype=np.complex128)
    )
    return CSIRecord(
        sequence=sequence,
        host_timestamp_ns=host_timestamp_ns,
        driver_timestamp=driver_timestamp,
        transmitter_address=transmitter_address,
        band=1,
        channel_frequency_mhz=frequency_mhz,
        channel_bandwidth=channel_bw_enum,
        data_bandwidth=data_bw_enum,
        rx_idx=rx_idx,
        tx_idx=tx_idx,
        samples=values,
        quality_flags=quality_flags,
        presence_flags=presence_flags,
        packet_sequence_number=(
            packet_number if presence_flags & CSI_PRESENT_PKT_SN else None
        ),
        segment_number=(
            segment_number if presence_flags & CSI_PRESENT_SEGMENT_NUM else None
        ),
        remain_last=(remain_last if presence_flags & CSI_PRESENT_REMAIN_LAST else None),
        transport_stream=(
            transport_stream if presence_flags & CSI_PRESENT_TR_STREAM else None
        ),
        h_idx=2,
        chain_info=3,
        rssi_raw=-48,
        snr_raw=29,
        primary_channel_index=primary_channel_index,
        rx_mode=rx_mode,
        rate_mcs=7,
        rate_nss=2,
        rate_guard_interval=1,
        rate_kbps=300_000,
        protocol_version=protocol_version,
    )


def make_datagram(
    *,
    sequence: int = 77,
    quality_flags: int = CSI_QUALITY_TONE_MASKED_REORDERED,
    presence_flags: int = ALL_PRESENCE,
    channel_bw_enum: int = 0,
    data_bw_enum: int = 0,
    frequency_mhz: int = 5500,
    remain_last: int = 0,
    rx_mode: int = 4,
    samples: tuple[tuple[int, int], ...] | None = None,
) -> bytes:
    count = EXPECTED_SAMPLE_COUNT_BY_DATA_BW[data_bw_enum]
    if samples is None:
        samples = tuple((100 + index, -20 + index) for index in range(count))
    message_size = V2_HEADER_SIZE + len(samples) * 4
    header = struct.pack(
        V2_HEADER_FORMAT,
        b"CSI2",
        2,
        quality_flags,
        V2_HEADER_SIZE,
        message_size,
        sequence,
        1_800_000_000_123_456_789,
        0x12345678,
        0x89ABCDEF,
        17,
        3,
        bytes.fromhex("021122334455"),
        -52,
        31,
        channel_bw_enum,
        data_bw_enum,
        0,
        1,
        rx_mode,
        7,
        0,
        1,
        len(samples),
        frequency_mhz,
        1,
        2,
        411,
        0,
        300_000,
        presence_flags,
        remain_last,
        0,
        1,
        0,
    )
    payload = b"".join(
        struct.pack(">hh", i_value, q_value) for i_value, q_value in samples
    )
    return header + payload


def manifest_for_records(
    records: list[CSIRecord], *, receiver_id: str = "receiver-a", session_id: str = "s"
) -> SessionManifest:
    groups = group_same_ppdu(records)
    return synthetic_session_manifest(
        groups, receiver_id=receiver_id, session_id=session_id
    )
