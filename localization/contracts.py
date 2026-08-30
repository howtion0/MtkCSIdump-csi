"""Hard analysis contract for Stage-2 CSI2 records.

Decoding a well-formed datagram is intentionally weaker than proving it is
safe for phase/delay analysis.  AoA, calibration, and CIR all call this module
and cannot bypass these gates with a low-confidence pairing option.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .csi2 import (
    CSI_PRESENT_BAND,
    CSI_PRESENT_CHANNEL_FREQ,
    CSI_PRESENT_REMAIN_LAST,
    CSI_PRESENT_RX_MODE,
    CSI_PRESENT_SEGMENT_NUM,
    CSI_QUALITY_CH_BW_INFERRED,
    CSI_QUALITY_DATA_NUM_INFERRED,
    CSI_QUALITY_FREQ_IS_PRIMARY,
    CSI_QUALITY_TONE_MASKED_REORDERED,
    CSI_QUALITY_TRUNCATED,
    EXPECTED_SAMPLE_COUNT_BY_DATA_BW,
    KNOWN_PRESENCE_MASK,
    KNOWN_QUALITY_MASK,
    V2_VERSION,
    bandwidth_enum_to_mhz,
)
from .models import CSIRecord

CANONICAL_TONE_MODE = "stage2-type5-masked-reordered-canonical"

# Exact mt76 enum values handled by the Stage-2 type-5 switch.  Other modes
# return before mask/reorder in the patched driver and therefore cannot be
# treated as canonical even if a capture-wide enable request succeeded.
RX_MODE_TONE_PROFILES = {
    1: ("mt76-type5-ofdm-csi-grid-312500hz", 312_500.0),
    2: ("mt76-type5-ht-csi-grid-312500hz", 312_500.0),
    4: ("mt76-type5-vht-csi-grid-312500hz", 312_500.0),
    8: ("mt76-type5-he-su-csi-grid-312500hz", 312_500.0),
}


def _tone_profile_for_group(base_profile: str, group: int) -> str:
    return base_profile if group == 0 else f"{base_profile}-mask-group-{group}"


def _tone_mask_group(record: CSIRecord) -> int:
    """Mirror the hardened driver's type-5 mask-group selection."""

    if (
        type(record.channel_bandwidth) is not int
        or type(record.data_bandwidth) is not int
        or record.channel_bandwidth not in {0, 1, 2}
        or record.data_bandwidth not in {0, 1, 2}
    ):
        raise AnalysisContractError("invalid bandwidth enum for tone-mask selection")
    primary_index = record.primary_channel_index
    if type(primary_index) is not int or not 0 <= primary_index <= 0xFF:
        raise AnalysisContractError("primary-channel index must be an exact uint8")
    if record.data_bandwidth == 1:
        primary_index //= 2
    if record.channel_bandwidth < 2 or record.data_bandwidth < 1:
        group = record.channel_bandwidth**2 + primary_index
    else:
        group = (
            record.channel_bandwidth**2
            + (record.data_bandwidth + 1) * 2
            + primary_index
        )
    if group >= 11:
        raise AnalysisContractError(
            "primary-channel index selects no audited type-5 tone-mask group"
        )
    return group


class AnalysisContractError(ValueError):
    """A record fails a non-negotiable real-data analysis gate."""


@dataclass(frozen=True)
class RadioToneConfig:
    band: int
    channel_frequency_mhz: int
    channel_bw_enum: int
    data_bw_enum: int
    sample_count: int
    rx_mode: int
    subcarrier_spacing_hz: float
    tone_mode: str
    tone_profile: str
    frequency_source: str

    def __post_init__(self) -> None:
        validate_radio_config(self)

    @property
    def channel_bw_mhz(self) -> int:
        return bandwidth_enum_to_mhz(self.channel_bw_enum)

    @property
    def data_bw_mhz(self) -> int:
        return bandwidth_enum_to_mhz(self.data_bw_enum)

    def signature(self) -> tuple[object, ...]:
        return (
            self.band,
            self.channel_frequency_mhz,
            self.channel_bw_enum,
            self.data_bw_enum,
            self.sample_count,
            self.rx_mode,
            self.subcarrier_spacing_hz,
            self.tone_mode,
            self.tone_profile,
            self.frequency_source,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "band": self.band,
            "channel_frequency_mhz": self.channel_frequency_mhz,
            "channel_bw_enum": self.channel_bw_enum,
            "channel_bw_mhz": self.channel_bw_mhz,
            "data_bw_enum": self.data_bw_enum,
            "data_bw_mhz": self.data_bw_mhz,
            "sample_count": self.sample_count,
            "rx_mode": self.rx_mode,
            "subcarrier_spacing_hz": self.subcarrier_spacing_hz,
            "tone_mode": self.tone_mode,
            "tone_profile": self.tone_profile,
            "frequency_source": self.frequency_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RadioToneConfig:
        integer_fields = (
            "band",
            "channel_frequency_mhz",
            "channel_bw_enum",
            "data_bw_enum",
            "sample_count",
            "rx_mode",
        )
        if any(type(data.get(name)) is not int for name in integer_fields):
            raise AnalysisContractError("radio-config numeric fields must be integers")
        if (
            type(data.get("tone_mode")) is not str
            or type(data.get("tone_profile")) is not str
            or type(data.get("frequency_source")) is not str
        ):
            raise AnalysisContractError("radio-config mode fields must be strings")
        spacing = data.get("subcarrier_spacing_hz")
        if type(spacing) not in {int, float} or not math.isfinite(float(spacing)):
            raise AnalysisContractError("subcarrier spacing must be finite")
        config = cls(
            band=data["band"],
            channel_frequency_mhz=data["channel_frequency_mhz"],
            channel_bw_enum=data["channel_bw_enum"],
            data_bw_enum=data["data_bw_enum"],
            sample_count=data["sample_count"],
            rx_mode=data["rx_mode"],
            subcarrier_spacing_hz=float(spacing),
            tone_mode=data["tone_mode"],
            tone_profile=data["tone_profile"],
            frequency_source=data["frequency_source"],
        )
        validate_radio_config(config)
        return config


def validate_radio_config(config: RadioToneConfig) -> None:
    integer_fields = (
        config.band,
        config.channel_frequency_mhz,
        config.channel_bw_enum,
        config.data_bw_enum,
        config.sample_count,
        config.rx_mode,
    )
    if any(type(value) is not int for value in integer_fields):
        raise AnalysisContractError("radio-config enum/count fields must be integers")
    if type(config.subcarrier_spacing_hz) not in {int, float} or not math.isfinite(
        config.subcarrier_spacing_hz
    ):
        raise AnalysisContractError("subcarrier spacing must be finite")
    if any(
        type(value) is not str or not value.strip()
        for value in (
            config.tone_mode,
            config.tone_profile,
            config.frequency_source,
        )
    ):
        raise AnalysisContractError(
            "radio-config mode fields must be non-empty strings"
        )
    try:
        bandwidth_enum_to_mhz(config.channel_bw_enum)
        bandwidth_enum_to_mhz(config.data_bw_enum)
    except ValueError as exc:
        raise AnalysisContractError(str(exc)) from exc
    if config.channel_bw_enum != config.data_bw_enum:
        raise AnalysisContractError(
            "channel_bw must equal data_bw until primary-subchannel mapping is audited"
        )
    expected = EXPECTED_SAMPLE_COUNT_BY_DATA_BW[config.data_bw_enum]
    if config.sample_count != expected:
        raise AnalysisContractError(
            f"canonical {config.data_bw_mhz} MHz CSI needs {expected} tones"
        )
    try:
        expected_profile, expected_spacing = RX_MODE_TONE_PROFILES[config.rx_mode]
    except KeyError as exc:
        raise AnalysisContractError(
            f"rx_mode {config.rx_mode} is not handled by the audited type-5 path"
        ) from exc
    allowed_groups = {
        0: {0},
        1: {1, 2},
        2: {10},
    }[config.data_bw_enum]
    allowed_profiles = {
        _tone_profile_for_group(expected_profile, group) for group in allowed_groups
    }
    if config.tone_profile not in allowed_profiles:
        raise AnalysisContractError("rx_mode and tone-profile provenance disagree")
    if (
        not math.isfinite(config.subcarrier_spacing_hz)
        or config.subcarrier_spacing_hz != expected_spacing
    ):
        raise AnalysisContractError("rx_mode and CSI-bin spacing provenance disagree")
    valid_frequency = (
        config.band == 0 and 2_400 <= config.channel_frequency_mhz <= 2_500
    ) or (config.band == 1 and 4_900 <= config.channel_frequency_mhz <= 5_900)
    if not valid_frequency:
        raise AnalysisContractError(
            "AX3000T band/frequency pair is outside the audited 2.4/5 GHz ranges"
        )
    if config.tone_mode != CANONICAL_TONE_MODE:
        raise AnalysisContractError("unknown or non-canonical CSI tone ordering")
    if config.frequency_source not in {"center", "primary"}:
        raise AnalysisContractError("unknown frequency source")
    if config.frequency_source == "primary" and config.data_bw_enum != 0:
        raise AnalysisContractError(
            "primary frequency is insufficient for 40/80 MHz tone coordinates"
        )


def validate_analysis_record(record: CSIRecord) -> RadioToneConfig:
    """Validate one record for real AoA/CIR use and return its config."""

    if (
        type(record.protocol_version) is not int
        or record.protocol_version != V2_VERSION
    ):
        raise AnalysisContractError("only the exact CSI2 protocol version is usable")
    if (
        type(record.quality_flags) is not int
        or not 0 <= record.quality_flags <= 0xFF
        or record.quality_flags & ~KNOWN_QUALITY_MASK
    ):
        raise AnalysisContractError("record contains unknown quality semantics")
    if (
        type(record.presence_flags) is not int
        or not 0 <= record.presence_flags <= 0xFFFF_FFFF
        or record.presence_flags & ~KNOWN_PRESENCE_MASK
    ):
        raise AnalysisContractError("record contains unknown presence semantics")
    if record.quality_flags & CSI_QUALITY_TRUNCATED:
        raise AnalysisContractError("truncated CSI is never analysis-usable")
    if record.quality_flags & (
        CSI_QUALITY_CH_BW_INFERRED | CSI_QUALITY_DATA_NUM_INFERRED
    ):
        raise AnalysisContractError(
            "inferred bandwidth or tone count is not phase/delay analysis-usable"
        )
    if not record.quality_flags & CSI_QUALITY_TONE_MASKED_REORDERED:
        raise AnalysisContractError(
            "Stage-2 TONE_MASKED_REORDERED is required for canonical tone order"
        )
    if not record.presence_flags & CSI_PRESENT_CHANNEL_FREQ:
        raise AnalysisContractError("channel-frequency presence bit is required")
    if not record.presence_flags & CSI_PRESENT_BAND:
        raise AnalysisContractError("band presence bit is required")
    if not record.presence_flags & CSI_PRESENT_SEGMENT_NUM:
        raise AnalysisContractError("segment provenance presence bit is required")
    if not record.presence_flags & CSI_PRESENT_REMAIN_LAST:
        raise AnalysisContractError("remain/last provenance presence bit is required")
    if not record.presence_flags & CSI_PRESENT_RX_MODE or record.rx_mode is None:
        raise AnalysisContractError("rx_mode presence and value are required")
    if (
        type(record.segment_number) is not int
        or not 0 <= record.segment_number <= 0xFFFF_FFFF
    ):
        raise AnalysisContractError("segment provenance must be an exact uint32")
    if type(record.remain_last) is not int or record.remain_last != 0:
        raise AnalysisContractError("Stage-2 CSI event is not a completed segment")
    if type(record.data_bandwidth) is not int:
        raise AnalysisContractError("data-bandwidth enum is absent")
    if record.channel_bandwidth != 2 and record.segment_number != 0:
        raise AnalysisContractError(
            "only reassembled 80 MHz records may carry a final segment index"
        )

    source = (
        "primary" if record.quality_flags & CSI_QUALITY_FREQ_IS_PRIMARY else "center"
    )
    try:
        tone_profile, subcarrier_spacing_hz = RX_MODE_TONE_PROFILES[record.rx_mode]
    except KeyError as exc:
        raise AnalysisContractError(
            f"rx_mode {record.rx_mode} is not handled by the audited type-5 path"
        ) from exc
    mask_group = _tone_mask_group(record)
    config = RadioToneConfig(
        band=record.band,
        channel_frequency_mhz=record.channel_frequency_mhz,
        channel_bw_enum=record.channel_bandwidth,
        data_bw_enum=record.data_bandwidth,
        sample_count=record.sample_count,
        rx_mode=record.rx_mode,
        subcarrier_spacing_hz=subcarrier_spacing_hz,
        tone_mode=CANONICAL_TONE_MODE,
        tone_profile=_tone_profile_for_group(tone_profile, mask_group),
        frequency_source=source,
    )
    validate_radio_config(config)
    return config
