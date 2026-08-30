"""Strict decoder for the Stage-1 CSI2 UDP wire format.

The decoder rejects truncated, oversized, internally inconsistent, or
unsupported datagrams.  It does not infer missing PPDU metadata from zeroes;
the corresponding fields become ``None`` unless their presence bit is set.
"""

from __future__ import annotations

import io
import struct
from collections.abc import Iterator
from typing import BinaryIO

import numpy as np

from .models import CSIRecord, normalize_mac

V2_MAGIC = b"CSI2"
V2_VERSION = 2
V2_HEADER_FORMAT = ">4sBBHIIQIIII6sbBBBBBBBHHHHBBHIIHBBBB"
V2_HEADER_SIZE = struct.calcsize(V2_HEADER_FORMAT)
V2_SAMPLE_FORMAT = ">i2"
V2_SAMPLE_BYTES = 4
V2_SAMPLE_FORMAT_SIGNED_IQ16 = 1
MAX_COMPLEX_SAMPLES = 1024
MAX_UDP_DATAGRAM_SIZE = 65_507

# MediaTek CSI attributes carry enum values, not MHz.  Stage 3 intentionally
# supports only the widths currently expected from the AX3000T path.
BANDWIDTH_ENUM_TO_MHZ = {0: 20, 1: 40, 2: 80}
EXPECTED_SAMPLE_COUNT_BY_DATA_BW = {0: 64, 1: 128, 2: 256}

CSI_QUALITY_CH_BW_INFERRED = 1 << 0
CSI_QUALITY_DATA_NUM_INFERRED = 1 << 1
CSI_QUALITY_EXTENDED_ABI = 1 << 2
CSI_QUALITY_TRUNCATED = 1 << 3
CSI_QUALITY_FREQ_IS_PRIMARY = 1 << 4
CSI_QUALITY_TONE_MASKED_REORDERED = 1 << 5

CSI_QUALITY_FLAG_NAMES = {
    CSI_QUALITY_CH_BW_INFERRED: "CH_BW_INFERRED",
    CSI_QUALITY_DATA_NUM_INFERRED: "DATA_NUM_INFERRED",
    CSI_QUALITY_EXTENDED_ABI: "EXTENDED_ABI",
    CSI_QUALITY_TRUNCATED: "TRUNCATED",
    CSI_QUALITY_FREQ_IS_PRIMARY: "FREQ_IS_PRIMARY",
    CSI_QUALITY_TONE_MASKED_REORDERED: "TONE_MASKED_REORDERED",
}
KNOWN_QUALITY_MASK = sum(CSI_QUALITY_FLAG_NAMES)

CSI_PRESENT_H_IDX = 1 << 0
CSI_PRESENT_CHAIN_INFO = 1 << 1
CSI_PRESENT_PKT_SN = 1 << 2
CSI_PRESENT_SEGMENT_NUM = 1 << 3
CSI_PRESENT_REMAIN_LAST = 1 << 4
CSI_PRESENT_TR_STREAM = 1 << 5
CSI_PRESENT_RX_MODE = 1 << 6
CSI_PRESENT_RATE_MCS = 1 << 7
CSI_PRESENT_RATE_NSS = 1 << 8
CSI_PRESENT_RATE_KBPS = 1 << 9
CSI_PRESENT_CHANNEL_FREQ = 1 << 10
CSI_PRESENT_BAND = 1 << 11
KNOWN_PRESENCE_MASK = (1 << 12) - 1


class CSI2ProtocolError(ValueError):
    """The datagram cannot be trusted as one complete CSI2 message."""


def quality_flag_names(flags: int) -> tuple[str, ...]:
    names = [name for bit, name in CSI_QUALITY_FLAG_NAMES.items() if flags & bit]
    known_mask = sum(CSI_QUALITY_FLAG_NAMES)
    unknown = flags & ~known_mask
    if unknown:
        names.append(f"UNKNOWN_QUALITY_BITS_0x{unknown:02x}")
    return tuple(names)


def bandwidth_enum_to_mhz(value: int) -> int:
    try:
        return BANDWIDTH_ENUM_TO_MHZ[int(value)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unsupported MediaTek bandwidth enum {value!r}") from exc


def bandwidth_mhz_to_enum(value: int) -> int:
    reverse = {mhz: enum for enum, mhz in BANDWIDTH_ENUM_TO_MHZ.items()}
    try:
        return reverse[int(value)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unsupported AX3000T bandwidth {value!r} MHz") from exc


def _optional(flags: int, bit: int, value: int) -> int | None:
    return value if flags & bit else None


def decode_csi2_datagram(data: bytes | bytearray | memoryview) -> CSIRecord:
    payload = memoryview(data)
    if len(payload) > MAX_UDP_DATAGRAM_SIZE:
        raise CSI2ProtocolError("datagram exceeds the IPv4 UDP payload limit")
    if len(payload) < V2_HEADER_SIZE:
        raise CSI2ProtocolError("datagram is shorter than the CSI2 header")

    try:
        fields = struct.unpack_from(V2_HEADER_FORMAT, payload)
    except struct.error as exc:
        raise CSI2ProtocolError("cannot unpack CSI2 header") from exc

    (
        magic,
        version,
        quality_flags,
        header_size,
        message_size,
        sequence,
        host_timestamp_ns,
        driver_timestamp,
        ext_info,
        h_idx,
        chain_info,
        transmitter_address,
        rssi,
        snr,
        channel_bandwidth,
        data_bandwidth,
        primary_channel_index,
        band,
        rx_mode,
        rate_mcs,
        tx_idx,
        rx_idx,
        data_num,
        channel_frequency_mhz,
        sample_format,
        rate_nss,
        packet_sequence_number,
        segment_number,
        rate_kbps,
        presence_flags,
        remain_last,
        transport_stream,
        rate_guard_interval,
        reserved,
    ) = fields

    if magic != V2_MAGIC or version != V2_VERSION:
        raise CSI2ProtocolError("unsupported CSI2 magic or version")
    if header_size != V2_HEADER_SIZE:
        raise CSI2ProtocolError(f"unsupported header size {header_size}")
    if message_size != len(payload):
        raise CSI2ProtocolError(
            f"message length mismatch: declared {message_size}, received {len(payload)}"
        )
    if sample_format != V2_SAMPLE_FORMAT_SIGNED_IQ16:
        raise CSI2ProtocolError(f"unsupported sample format {sample_format}")
    if reserved != 0:
        raise CSI2ProtocolError("CSI2 reserved byte must be zero")
    if quality_flags & ~KNOWN_QUALITY_MASK:
        raise CSI2ProtocolError("CSI2 contains unknown quality semantics")
    if presence_flags & ~KNOWN_PRESENCE_MASK:
        raise CSI2ProtocolError("CSI2 contains unknown presence semantics")
    if quality_flags & CSI_QUALITY_TRUNCATED:
        raise CSI2ProtocolError("driver marked the CSI report truncated")
    try:
        bandwidth_enum_to_mhz(channel_bandwidth)
        bandwidth_enum_to_mhz(data_bandwidth)
    except ValueError as exc:
        raise CSI2ProtocolError(str(exc)) from exc
    if not 1 <= data_num <= MAX_COMPLEX_SAMPLES:
        raise CSI2ProtocolError("data_num is outside the supported CSI range")
    expected_data_num = EXPECTED_SAMPLE_COUNT_BY_DATA_BW[data_bandwidth]
    if data_num != expected_data_num:
        raise CSI2ProtocolError(
            "data_num does not match data-BW enum: "
            f"expected {expected_data_num}, got {data_num}"
        )
    if header_size + data_num * V2_SAMPLE_BYTES != message_size:
        raise CSI2ProtocolError("data_num does not match the payload length")

    iq = np.frombuffer(payload[header_size:], dtype=V2_SAMPLE_FORMAT).astype(np.int16)
    if iq.size != data_num * 2:
        raise CSI2ProtocolError("I/Q payload has an odd or incomplete sample count")
    samples = iq[0::2].astype(float) + 1j * iq[1::2].astype(float)

    return CSIRecord(
        sequence=sequence,
        host_timestamp_ns=host_timestamp_ns,
        driver_timestamp=driver_timestamp,
        transmitter_address=normalize_mac(transmitter_address),
        band=band,
        channel_frequency_mhz=(
            channel_frequency_mhz if presence_flags & CSI_PRESENT_CHANNEL_FREQ else 0
        ),
        channel_bandwidth=channel_bandwidth,
        rx_idx=rx_idx,
        tx_idx=tx_idx,
        samples=samples,
        quality_flags=quality_flags,
        presence_flags=presence_flags,
        packet_sequence_number=_optional(
            presence_flags, CSI_PRESENT_PKT_SN, packet_sequence_number
        ),
        segment_number=_optional(
            presence_flags, CSI_PRESENT_SEGMENT_NUM, segment_number
        ),
        remain_last=_optional(presence_flags, CSI_PRESENT_REMAIN_LAST, remain_last),
        transport_stream=_optional(
            presence_flags, CSI_PRESENT_TR_STREAM, transport_stream
        ),
        h_idx=_optional(presence_flags, CSI_PRESENT_H_IDX, h_idx),
        chain_info=_optional(presence_flags, CSI_PRESENT_CHAIN_INFO, chain_info),
        rssi_raw=rssi,
        snr_raw=snr,
        data_bandwidth=data_bandwidth,
        primary_channel_index=primary_channel_index,
        rx_mode=_optional(presence_flags, CSI_PRESENT_RX_MODE, rx_mode),
        rate_mcs=_optional(presence_flags, CSI_PRESENT_RATE_MCS, rate_mcs),
        rate_nss=_optional(presence_flags, CSI_PRESENT_RATE_NSS, rate_nss),
        rate_guard_interval=rate_guard_interval,
        rate_kbps=_optional(presence_flags, CSI_PRESENT_RATE_KBPS, rate_kbps),
        ext_info=ext_info,
    )


def iter_length_prefixed_csi2(
    source: BinaryIO | bytes | bytearray | memoryview,
) -> Iterator[CSIRecord]:
    """Read ``u32-be length + CSI2 datagram`` capture files.

    Length framing is a host-side recording convention, not part of CSI2 UDP.
    It prevents accidental packet-boundary guesses when replaying a capture.
    """

    stream: BinaryIO
    if isinstance(source, (bytes, bytearray, memoryview)):
        stream = io.BytesIO(bytes(source))
    else:
        stream = source

    while True:
        prefix = stream.read(4)
        if not prefix:
            return
        if len(prefix) != 4:
            raise CSI2ProtocolError("truncated length prefix")
        (length,) = struct.unpack(">I", prefix)
        if not V2_HEADER_SIZE <= length <= MAX_UDP_DATAGRAM_SIZE:
            raise CSI2ProtocolError(f"invalid framed datagram length {length}")
        datagram = stream.read(length)
        if len(datagram) != length:
            raise CSI2ProtocolError("truncated framed CSI2 datagram")
        yield decode_csi2_datagram(datagram)
