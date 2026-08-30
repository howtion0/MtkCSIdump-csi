from __future__ import annotations

import hashlib
import io
import json
import struct
from pathlib import Path

import pytest

from localization.contracts import AnalysisContractError, validate_analysis_record
from localization.csi2 import (
    CSI_PRESENT_CHANNEL_FREQ,
    CSI_PRESENT_RX_MODE,
    CSI_QUALITY_CH_BW_INFERRED,
    CSI_QUALITY_DATA_NUM_INFERRED,
    CSI_QUALITY_FREQ_IS_PRIMARY,
    CSI_QUALITY_TONE_MASKED_REORDERED,
    CSI_QUALITY_TRUNCATED,
    V2_HEADER_FORMAT,
    V2_HEADER_SIZE,
    CSI2ProtocolError,
    bandwidth_enum_to_mhz,
    decode_csi2_datagram,
    iter_length_prefixed_csi2,
)
from tests.helpers import ALL_PRESENCE, make_datagram, make_record


def _replace_header_field(datagram: bytes, index: int, value: object) -> bytes:
    fields = list(struct.unpack(V2_HEADER_FORMAT, datagram[:V2_HEADER_SIZE]))
    fields[index] = value
    return struct.pack(V2_HEADER_FORMAT, *fields) + datagram[V2_HEADER_SIZE:]


@pytest.mark.parametrize(("bw_enum", "count"), [(0, 64), (1, 128), (2, 256)])
def test_decoder_keeps_raw_bw_quality_and_exact_tone_count(
    bw_enum: int, count: int
) -> None:
    record = decode_csi2_datagram(
        make_datagram(channel_bw_enum=bw_enum, data_bw_enum=bw_enum)
    )
    assert record.channel_bandwidth == bw_enum
    assert record.data_bandwidth == bw_enum
    assert record.channel_bandwidth_mhz == bandwidth_enum_to_mhz(bw_enum)
    assert record.sample_count == count
    assert record.quality_flags == CSI_QUALITY_TONE_MASKED_REORDERED
    assert record.rssi_raw == -52
    assert record.snr_raw == 31


def test_truncated_quality_bit_is_a_decoder_hard_reject() -> None:
    with pytest.raises(CSI2ProtocolError, match="truncated"):
        decode_csi2_datagram(
            make_datagram(
                quality_flags=(
                    CSI_QUALITY_TONE_MASKED_REORDERED | CSI_QUALITY_TRUNCATED
                )
            )
        )


def test_decoder_rejects_noncanonical_count_and_length_mismatch() -> None:
    with pytest.raises(CSI2ProtocolError, match="data-BW enum"):
        decode_csi2_datagram(make_datagram(samples=((1, 2),) * 63))
    message = bytearray(make_datagram())
    struct.pack_into(">I", message, 8, len(message) + 4)
    with pytest.raises(CSI2ProtocolError, match="length mismatch"):
        decode_csi2_datagram(message)


@pytest.mark.parametrize(
    ("field_index", "value", "message"),
    [
        (33, 1, "reserved byte"),
        (2, 1 << 6, "unknown quality"),
        (29, ALL_PRESENCE | (1 << 12), "unknown presence"),
    ],
)
def test_decoder_rejects_reserved_and_unknown_protocol_semantics(
    field_index: int, value: int, message: str
) -> None:
    with pytest.raises(CSI2ProtocolError, match=message):
        decode_csi2_datagram(_replace_header_field(make_datagram(), field_index, value))


def test_framing_preserves_packet_boundaries_and_rejects_truncation() -> None:
    packet = make_datagram()
    framed = struct.pack(">I", len(packet)) + packet
    records = list(iter_length_prefixed_csi2(framed + framed))
    assert [record.sequence for record in records] == [77, 77]
    with pytest.raises(CSI2ProtocolError, match="truncated framed"):
        list(iter_length_prefixed_csi2(io.BytesIO(framed[:-1])))


def test_analysis_contract_requires_stage2_canonical_tones_and_presence() -> None:
    with pytest.raises(AnalysisContractError, match="TONE_MASKED_REORDERED"):
        validate_analysis_record(make_record(rx_idx=0, quality_flags=0))
    with pytest.raises(AnalysisContractError, match="frequency presence"):
        validate_analysis_record(
            make_record(
                rx_idx=0, presence_flags=ALL_PRESENCE & ~CSI_PRESENT_CHANNEL_FREQ
            )
        )
    with pytest.raises(AnalysisContractError, match="rx_mode presence"):
        validate_analysis_record(
            make_record(rx_idx=0, presence_flags=ALL_PRESENCE & ~CSI_PRESENT_RX_MODE)
        )
    with pytest.raises(AnalysisContractError, match="not handled"):
        validate_analysis_record(make_record(rx_idx=0, rx_mode=3))
    for inferred_flag in (
        CSI_QUALITY_CH_BW_INFERRED,
        CSI_QUALITY_DATA_NUM_INFERRED,
    ):
        with pytest.raises(AnalysisContractError, match="inferred bandwidth"):
            validate_analysis_record(
                make_record(
                    rx_idx=0,
                    quality_flags=(CSI_QUALITY_TONE_MASKED_REORDERED | inferred_flag),
                )
            )
    with pytest.raises(AnalysisContractError, match="band/frequency"):
        validate_analysis_record(make_record(rx_idx=0, frequency_mhz=2412))
    with pytest.raises(AnalysisContractError, match="channel_bw must equal data_bw"):
        validate_analysis_record(
            make_record(
                rx_idx=0,
                channel_bw_enum=1,
                data_bw_enum=0,
            )
        )


def test_direct_records_cannot_bypass_protocol_or_known_bit_contracts() -> None:
    with pytest.raises(AnalysisContractError, match="exact CSI2 protocol"):
        validate_analysis_record(make_record(rx_idx=0, protocol_version=1))
    with pytest.raises(AnalysisContractError, match="unknown quality"):
        validate_analysis_record(
            make_record(
                rx_idx=0,
                quality_flags=CSI_QUALITY_TONE_MASKED_REORDERED | (1 << 7),
            )
        )
    with pytest.raises(AnalysisContractError, match="unknown presence"):
        validate_analysis_record(
            make_record(rx_idx=0, presence_flags=ALL_PRESENCE | (1 << 12))
        )


def test_primary_frequency_is_only_accepted_for_20_mhz() -> None:
    record_20 = make_record(
        rx_idx=0,
        quality_flags=(CSI_QUALITY_TONE_MASKED_REORDERED | CSI_QUALITY_FREQ_IS_PRIMARY),
    )
    assert validate_analysis_record(record_20).frequency_source == "primary"
    record_40 = make_record(
        rx_idx=0,
        channel_bw_enum=1,
        data_bw_enum=1,
        quality_flags=(CSI_QUALITY_TONE_MASKED_REORDERED | CSI_QUALITY_FREQ_IS_PRIMARY),
    )
    with pytest.raises(AnalysisContractError, match="insufficient for 40/80"):
        validate_analysis_record(record_40)


def test_segment_semantics_match_the_hardened_reassembly_contract() -> None:
    with pytest.raises(AnalysisContractError, match="only reassembled 80 MHz"):
        validate_analysis_record(make_record(rx_idx=0, segment_number=7))
    with pytest.raises(AnalysisContractError, match="only reassembled 80 MHz"):
        validate_analysis_record(
            make_record(
                rx_idx=0,
                channel_bw_enum=1,
                data_bw_enum=1,
                segment_number=1,
            )
        )
    record_80 = make_record(
        rx_idx=0,
        channel_bw_enum=2,
        data_bw_enum=2,
        segment_number=7,
    )
    assert validate_analysis_record(record_80).sample_count == 256
    with pytest.raises(AnalysisContractError, match="not a completed segment"):
        validate_analysis_record(make_record(rx_idx=0, remain_last=1))


def test_primary_index_is_bound_to_the_exact_type5_tone_mask_group() -> None:
    with pytest.raises(AnalysisContractError, match="tone-profile provenance"):
        validate_analysis_record(make_record(rx_idx=0, primary_channel_index=1))
    lower_40 = validate_analysis_record(
        make_record(
            rx_idx=0,
            channel_bw_enum=1,
            data_bw_enum=1,
            primary_channel_index=0,
        )
    )
    upper_40 = validate_analysis_record(
        make_record(
            rx_idx=0,
            channel_bw_enum=1,
            data_bw_enum=1,
            primary_channel_index=2,
        )
    )
    assert lower_40.tone_profile.endswith("mask-group-1")
    assert upper_40.tone_profile.endswith("mask-group-2")
    assert lower_40.signature() != upper_40.signature()
    with pytest.raises(AnalysisContractError, match="no audited type-5"):
        validate_analysis_record(
            make_record(
                rx_idx=0,
                channel_bw_enum=2,
                data_bw_enum=2,
                primary_channel_index=1,
            )
        )


def test_stage1_encoder_fixture_decodes_differentially() -> None:
    fixture_dir = Path(__file__).parent / "fixtures"
    provenance = json.loads(
        (fixture_dir / "stage1_encoder_v2.provenance.json").read_text()
    )
    payload = (fixture_dir / "stage1_encoder_v2.csi2").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == provenance["fixture_sha256"]
    assert len(payload) == V2_HEADER_SIZE + 64 * 4
    record = decode_csi2_datagram(payload)
    expected = provenance["expected"]
    assert record.sequence == expected["sequence"]
    assert record.host_timestamp_ns == expected["host_timestamp_ns"]
    assert record.driver_timestamp == expected["driver_timestamp"]
    assert record.transmitter_address == expected["transmitter_address"]
    assert record.quality_flags == expected["quality_flags"]
    assert record.presence_flags == expected["presence_flags"]
    assert record.ext_info == expected["ext_info"]
    assert record.h_idx == expected["h_idx"]
    assert record.chain_info == expected["chain_info"]
    assert record.rssi_raw == expected["rssi_raw"]
    assert record.snr_raw == expected["snr_raw"]
    assert record.band == expected["band"]
    assert record.channel_bandwidth == expected["channel_bandwidth_enum"]
    assert record.data_bandwidth == expected["data_bandwidth_enum"]
    assert record.channel_frequency_mhz == expected["channel_frequency_mhz"]
    assert record.rx_mode == expected["rx_mode"]
    assert record.rate_mcs == expected["rate_mcs"]
    assert record.rate_nss == expected["rate_nss"]
    assert record.rate_guard_interval == expected["rate_guard_interval"]
    assert record.rate_kbps == expected["rate_kbps"]
    assert record.packet_sequence_number == expected["packet_sequence_number"]
    assert record.segment_number == expected["segment_number"]
    assert record.remain_last == expected["remain_last"]
    assert record.transport_stream == expected["transport_stream"]
    assert record.tx_idx == expected["tx_idx"]
    assert record.rx_idx == expected["rx_idx"]
    assert record.primary_channel_index == expected["primary_channel_index"]
    assert record.sample_count == expected["sample_count"]
    assert [record.samples[0].real, record.samples[0].imag] == expected["first_iq"]
    assert [record.samples[-1].real, record.samples[-1].imag] == expected["last_iq"]
