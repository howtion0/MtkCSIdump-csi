"""Receive-chain mapping and circular complex-ratio calibration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .contracts import RadioToneConfig
from .grouping import GroupedPPDU
from .jsonio import dump_json, json_safe, load_json
from .models import CSIRecord
from .session import SessionManifest, windows_overlap

SPEED_OF_LIGHT_M_S = 299_792_458.0
MIN_AOA_BASELINE_M = 0.020
MAX_CROSS_ANGLE_MEDIAN_RESIDUAL_RAD = 0.35
MAX_CROSS_ANGLE_P90_RESIDUAL_RAD = 0.70
MIN_PER_CAPTURE_MEDIAN_CONCENTRATION = 0.70
MIN_CROSS_ANGLE_STEERING_SEPARATION_RAD = 0.70


def _json_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a JSON integer")
    return value


def _json_number(value: object, name: str) -> float:
    if type(value) not in {int, float} or not np.isfinite(value):
        raise ValueError(f"{name} must be a finite JSON number")
    return float(value)


def _json_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a JSON string")
    return value


def _json_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


@dataclass(frozen=True)
class AntennaElement:
    chain_id: int
    name: str
    position_m: tuple[float, float]
    connector: str = "unknown"

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or not 0 <= self.chain_id <= 0xFFFF:
            raise ValueError("antenna chain_id must be uint16")
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("antenna name is required")
        if type(self.connector) is not str or not self.connector.strip():
            raise ValueError("antenna connector description is required")
        if len(self.position_m) != 2 or any(
            type(value) not in {int, float} or not np.isfinite(value)
            for value in self.position_m
        ):
            raise ValueError("antenna position_m must contain two finite numbers")

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AntennaElement:
        position_data = data.get("position_m")
        if type(position_data) is not list or len(position_data) != 2:
            raise ValueError("antenna position_m must contain x and y")
        position = tuple(
            _json_number(value, f"position_m[{index}]")
            for index, value in enumerate(position_data)
        )
        return cls(
            chain_id=_json_int(data["chain_id"], "chain_id"),
            name=_json_str(data["name"], "name"),
            position_m=(position[0], position[1]),
            connector=_json_str(data.get("connector", "unknown"), "connector"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "name": self.name,
            "position_m": list(self.position_m),
            "connector": self.connector,
        }


@dataclass(frozen=True)
class ChainMapping:
    receiver_id: str
    reference_rx_idx: int
    target_rx_idx: int
    elements: tuple[AntennaElement, ...]
    tx_idx: int | None = None
    transport_stream: int | None = None
    require_transport_stream_absent: bool = False
    array_broadside_heading_deg: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.reference_rx_idx) is not int
            or type(self.target_rx_idx) is not int
        ):
            raise ValueError("RX selectors must be exact integers")
        if (
            type(self.elements) is not tuple
            or len(self.elements) < 2
            or any(not isinstance(element, AntennaElement) for element in self.elements)
        ):
            raise ValueError("chain mapping needs at least two antenna elements")
        by_chain = {element.chain_id: element for element in self.elements}
        if len(by_chain) != len(self.elements):
            raise ValueError("chain mapping contains duplicate chain ids")
        if self.reference_rx_idx == self.target_rx_idx:
            raise ValueError("reference and target RX indices must differ")
        if self.reference_rx_idx not in by_chain or self.target_rx_idx not in by_chain:
            raise ValueError("selected RX pair is missing from the mapping")
        if not self.receiver_id.strip():
            raise ValueError("receiver_id is required")
        if type(self.tx_idx) is not int or not 0 <= self.tx_idx <= 0xFFFF:
            raise ValueError("an exact uint16 tx_idx selector is required")
        if type(self.require_transport_stream_absent) is not bool:
            raise ValueError("require_transport_stream_absent must be boolean")
        selector_count = int(self.transport_stream is not None) + int(
            self.require_transport_stream_absent
        )
        if selector_count != 1:
            raise ValueError(
                "select exactly one transport stream or explicitly require it absent"
            )
        if self.transport_stream is not None and (
            type(self.transport_stream) is not int
            or not 0 <= self.transport_stream <= 0xFF
        ):
            raise ValueError("transport_stream selector must be uint8")
        if (
            not 0 <= self.reference_rx_idx <= 0xFFFF
            or not 0 <= self.target_rx_idx <= 0xFFFF
        ):
            raise ValueError("RX selectors must be uint16")
        if type(self.array_broadside_heading_deg) not in {
            int,
            float,
        } or not np.isfinite(self.array_broadside_heading_deg):
            raise ValueError("broadside heading must be finite")
        if type(self.notes) is not str:
            raise ValueError("mapping notes must be a string")
        if not np.isfinite(self.spacing_m) or self.spacing_m < MIN_AOA_BASELINE_M:
            raise ValueError(
                f"selected antenna spacing must be at least {MIN_AOA_BASELINE_M:.3f} m"
            )
        broadside = np.deg2rad(self.array_broadside_heading_deg)
        broadside_unit = np.asarray([np.cos(broadside), np.sin(broadside)])
        alignment = abs(float(np.dot(self.baseline_vector_m, broadside_unit)))
        if alignment / self.spacing_m > np.sin(np.deg2rad(15.0)):
            raise ValueError(
                "antenna baseline must be within 15 degrees of perpendicular to broadside"
            )

    @property
    def spacing_m(self) -> float:
        return float(np.linalg.norm(self.baseline_vector_m))

    @property
    def baseline_vector_m(self) -> np.ndarray:
        by_chain = {element.chain_id: element for element in self.elements}
        left = np.asarray(by_chain[self.reference_rx_idx].position_m, dtype=float)
        right = np.asarray(by_chain[self.target_rx_idx].position_m, dtype=float)
        return right - left

    def to_dict(self) -> dict[str, object]:
        return {
            "receiver_id": self.receiver_id,
            "reference_rx_idx": self.reference_rx_idx,
            "target_rx_idx": self.target_rx_idx,
            "tx_idx": self.tx_idx,
            "transport_stream": self.transport_stream,
            "require_transport_stream_absent": self.require_transport_stream_absent,
            "array_broadside_heading_deg": self.array_broadside_heading_deg,
            "elements": [element.to_dict() for element in self.elements],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ChainMapping:
        # Old field names are accepted only as a migration convenience.  Saved
        # artifacts always use receiver/RX terminology and explicit selectors.
        absent_selector = _json_bool(
            data.get("require_transport_stream_absent", False),
            "require_transport_stream_absent",
        )
        reference_value = data.get("reference_rx_idx", data.get("reference_chain"))
        target_value = data.get("target_rx_idx", data.get("target_chain"))
        tx_value = data.get("tx_idx")
        stream_value = data.get("transport_stream")
        elements_data = data.get("elements")
        if type(elements_data) is not list:
            raise ValueError("elements must be a JSON array")
        return cls(
            receiver_id=_json_str(
                data.get("receiver_id", data.get("device_id", "")), "receiver_id"
            ),
            reference_rx_idx=_json_int(reference_value, "reference_rx_idx"),
            target_rx_idx=_json_int(target_value, "target_rx_idx"),
            tx_idx=(None if tx_value is None else _json_int(tx_value, "tx_idx")),
            transport_stream=(
                None
                if stream_value is None
                else _json_int(stream_value, "transport_stream")
            ),
            require_transport_stream_absent=absent_selector,
            array_broadside_heading_deg=_json_number(
                data.get("array_broadside_heading_deg", 0.0),
                "array_broadside_heading_deg",
            ),
            elements=tuple(AntennaElement.from_dict(item) for item in elements_data),
            notes=_json_str(data.get("notes", ""), "notes"),
        )


def _validate_calibration_angles(primary_deg: float, validation_deg: float) -> None:
    if (
        type(primary_deg) not in {int, float}
        or type(validation_deg) not in {int, float}
        or not np.isfinite(primary_deg)
        or not np.isfinite(validation_deg)
    ):
        raise ValueError("calibration angles must be finite")
    if abs(primary_deg) < 10.0 or abs(validation_deg) < 10.0:
        raise ValueError("both calibration angles must be at least 10 degrees")
    if abs(primary_deg) > 75.0 or abs(validation_deg) > 75.0:
        raise ValueError(
            "calibration angles must stay inside the ±75 degree far-field gate"
        )
    if primary_deg * validation_deg >= 0:
        raise ValueError("calibration captures must be on opposite sides of broadside")
    if abs(primary_deg - validation_deg) < 20.0:
        raise ValueError("calibration angles need at least 20 degrees separation")


@dataclass
class ChainCalibration:
    """Frequency-dependent hardware ratio bound to one receiver radio epoch."""

    calibration_id: str
    mapping: ChainMapping
    session_manifest: SessionManifest
    validation_session_manifest: SessionManifest
    subcarrier_spacing_hz: float
    complex_ratio: np.ndarray
    phase_concentration: np.ndarray
    known_angle_deg: float
    validation_angle_deg: float
    packet_count: int
    validation_packet_count: int
    cross_angle_phase_residual_median_rad: float
    cross_angle_phase_residual_p90_rad: float
    created_utc: str
    minimum_valid_tone_fraction: float
    synthetic: bool = False
    schema_version: int = 3

    def __post_init__(self) -> None:
        if self.schema_version != 3:
            raise ValueError("unsupported calibration schema version")
        if type(self.synthetic) is not bool:
            raise ValueError("synthetic must be boolean")
        if not isinstance(self.mapping, ChainMapping):
            raise TypeError("calibration mapping has the wrong type")
        self.complex_ratio = np.asarray(self.complex_ratio, dtype=np.complex128)
        self.phase_concentration = np.asarray(self.phase_concentration, dtype=float)
        if type(self.calibration_id) is not str or not self.calibration_id.strip():
            raise ValueError("calibration_id is required")
        if self.mapping.receiver_id != self.session_manifest.receiver_id:
            raise ValueError("mapping receiver_id differs from calibration session")
        if self.mapping.receiver_id != self.validation_session_manifest.receiver_id:
            raise ValueError("mapping receiver_id differs from validation session")
        if (
            self.synthetic != self.session_manifest.synthetic
            or self.synthetic != self.validation_session_manifest.synthetic
        ):
            raise ValueError(
                "calibration synthetic provenance must match both session manifests"
            )
        self.session_manifest.assert_radio_epoch_compatible(
            self.validation_session_manifest
        )
        if self.complex_ratio.ndim != 1 or self.complex_ratio.size == 0:
            raise ValueError("complex calibration ratio must be one-dimensional")
        if self.phase_concentration.shape != self.complex_ratio.shape:
            raise ValueError("phase concentration shape must match calibration ratio")
        if self.complex_ratio.size != self.session_manifest.radio_config.sample_count:
            raise ValueError("calibration length differs from session tone contract")
        if not np.all(np.isfinite(self.complex_ratio)):
            raise ValueError("calibration contains non-finite complex values")
        if np.any(np.abs(self.complex_ratio) < 1e-12):
            raise ValueError("calibration contains a zero complex ratio")
        if not np.all(np.isfinite(self.phase_concentration)) or np.any(
            (self.phase_concentration < 0) | (self.phase_concentration > 1)
        ):
            raise ValueError("phase concentration must be in [0, 1]")
        if (
            type(self.packet_count) is not int
            or type(self.validation_packet_count) is not int
            or self.packet_count <= 0
            or self.validation_packet_count <= 0
        ):
            raise ValueError("both calibration packet counts must be positive")
        if (
            type(self.minimum_valid_tone_fraction) not in {int, float}
            or not np.isfinite(self.minimum_valid_tone_fraction)
            or not 0 < self.minimum_valid_tone_fraction <= 1
        ):
            raise ValueError("minimum_valid_tone_fraction must be in (0, 1]")
        if (
            type(self.subcarrier_spacing_hz) not in {int, float}
            or type(self.known_angle_deg) not in {int, float}
            or type(self.validation_angle_deg) not in {int, float}
            or not np.isfinite(self.subcarrier_spacing_hz)
            or self.subcarrier_spacing_hz <= 0
            or not np.isfinite(self.known_angle_deg)
            or not np.isfinite(self.validation_angle_deg)
        ):
            raise ValueError("calibration frequency/angle values must be finite")
        if self.subcarrier_spacing_hz != self.radio_config.subcarrier_spacing_hz:
            raise ValueError("calibration spacing differs from the bound radio profile")
        if type(self.created_utc) is not str or not self.created_utc.strip():
            raise ValueError("calibration created_utc is required")
        try:
            datetime.fromisoformat(self.created_utc)
        except ValueError as exc:
            raise ValueError("calibration created_utc must be ISO-8601") from exc
        _validate_calibration_angles(self.known_angle_deg, self.validation_angle_deg)
        _validate_cross_angle_steering_separation(
            self.mapping,
            self.radio_config,
            self.known_angle_deg,
            self.validation_angle_deg,
        )
        residuals = (
            self.cross_angle_phase_residual_median_rad,
            self.cross_angle_phase_residual_p90_rad,
        )
        if any(
            type(value) not in {int, float} or not np.isfinite(value) or value < 0
            for value in residuals
        ):
            raise ValueError(
                "cross-angle residual metrics must be finite and non-negative"
            )
        if (
            self.cross_angle_phase_residual_median_rad
            > MAX_CROSS_ANGLE_MEDIAN_RESIDUAL_RAD
            or self.cross_angle_phase_residual_p90_rad
            > MAX_CROSS_ANGLE_P90_RESIDUAL_RAD
        ):
            raise ValueError("cross-angle calibration residual exceeds the hard gate")
        if self.calibration_id != self.computed_artifact_id():
            raise ValueError("calibration_id does not match calibration artifact hash")

    @property
    def sample_count(self) -> int:
        return int(self.complex_ratio.size)

    @property
    def radio_config(self) -> RadioToneConfig:
        return self.session_manifest.radio_config

    def computed_artifact_id(self) -> str:
        payload = {
            "schema": "ax3000t-chain-calibration",
            "schema_version": self.schema_version,
            "mapping": self.mapping.to_dict(),
            "session_manifest": self.session_manifest.to_dict(),
            "validation_session_manifest": self.validation_session_manifest.to_dict(),
            "subcarrier_spacing_hz": self.subcarrier_spacing_hz,
            "known_angle_deg": self.known_angle_deg,
            "validation_angle_deg": self.validation_angle_deg,
            "packet_count": self.packet_count,
            "validation_packet_count": self.validation_packet_count,
            "cross_angle_phase_residual_median_rad": self.cross_angle_phase_residual_median_rad,
            "cross_angle_phase_residual_p90_rad": self.cross_angle_phase_residual_p90_rad,
            "minimum_valid_tone_fraction": self.minimum_valid_tone_fraction,
            "created_utc": self.created_utc,
            "synthetic": self.synthetic,
            "complex_ratio": [
                {"real": float(value.real), "imag": float(value.imag)}
                for value in self.complex_ratio
            ],
            "phase_concentration": self.phase_concentration.tolist(),
        }
        encoded = json.dumps(
            json_safe(payload),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @property
    def band(self) -> int:
        return self.radio_config.band

    @property
    def channel_frequency_mhz(self) -> int:
        return self.radio_config.channel_frequency_mhz

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "ax3000t-chain-calibration",
            "schema_version": self.schema_version,
            "calibration_id": self.calibration_id,
            "mapping": self.mapping.to_dict(),
            "session_manifest": self.session_manifest.to_dict(),
            "validation_session_manifest": self.validation_session_manifest.to_dict(),
            "subcarrier_spacing_hz": self.subcarrier_spacing_hz,
            "known_angle_deg": self.known_angle_deg,
            "validation_angle_deg": self.validation_angle_deg,
            "packet_count": self.packet_count,
            "validation_packet_count": self.validation_packet_count,
            "cross_angle_phase_residual_median_rad": self.cross_angle_phase_residual_median_rad,
            "cross_angle_phase_residual_p90_rad": self.cross_angle_phase_residual_p90_rad,
            "minimum_valid_tone_fraction": self.minimum_valid_tone_fraction,
            "created_utc": self.created_utc,
            "synthetic": self.synthetic,
            "complex_ratio": [
                {"real": float(value.real), "imag": float(value.imag)}
                for value in self.complex_ratio
            ],
            "phase_concentration": self.phase_concentration.tolist(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ChainCalibration:
        if (
            data.get("schema") != "ax3000t-chain-calibration"
            or _json_int(data.get("schema_version"), "schema_version") != 3
        ):
            raise ValueError("unsupported calibration schema")
        ratio_data = data.get("complex_ratio")
        concentration_data = data.get("phase_concentration")
        if type(ratio_data) is not list or not ratio_data:
            raise ValueError("complex_ratio must be a non-empty JSON array")
        if type(concentration_data) is not list:
            raise ValueError("phase_concentration must be a JSON array")
        ratio_values: list[complex] = []
        for index, item in enumerate(ratio_data):
            if type(item) is not dict or set(item) != {"real", "imag"}:
                raise ValueError("complex_ratio items must contain only real/imag")
            ratio_values.append(
                complex(
                    _json_number(item["real"], f"complex_ratio[{index}].real"),
                    _json_number(item["imag"], f"complex_ratio[{index}].imag"),
                )
            )
        ratio = np.asarray(ratio_values, dtype=np.complex128)
        return cls(
            calibration_id=_json_str(data["calibration_id"], "calibration_id"),
            mapping=ChainMapping.from_dict(data["mapping"]),
            session_manifest=SessionManifest.from_dict(data["session_manifest"]),
            validation_session_manifest=SessionManifest.from_dict(
                data["validation_session_manifest"]
            ),
            subcarrier_spacing_hz=_json_number(
                data["subcarrier_spacing_hz"], "subcarrier_spacing_hz"
            ),
            complex_ratio=ratio,
            phase_concentration=np.asarray(
                [
                    _json_number(value, f"phase_concentration[{index}]")
                    for index, value in enumerate(concentration_data)
                ],
                dtype=float,
            ),
            known_angle_deg=_json_number(data["known_angle_deg"], "known_angle_deg"),
            validation_angle_deg=_json_number(
                data["validation_angle_deg"], "validation_angle_deg"
            ),
            packet_count=_json_int(data["packet_count"], "packet_count"),
            validation_packet_count=_json_int(
                data["validation_packet_count"], "validation_packet_count"
            ),
            cross_angle_phase_residual_median_rad=_json_number(
                data["cross_angle_phase_residual_median_rad"],
                "cross_angle_phase_residual_median_rad",
            ),
            cross_angle_phase_residual_p90_rad=_json_number(
                data["cross_angle_phase_residual_p90_rad"],
                "cross_angle_phase_residual_p90_rad",
            ),
            minimum_valid_tone_fraction=_json_number(
                data["minimum_valid_tone_fraction"], "minimum_valid_tone_fraction"
            ),
            created_utc=_json_str(data["created_utc"], "created_utc"),
            synthetic=_json_bool(data.get("synthetic", False), "synthetic"),
            schema_version=_json_int(data["schema_version"], "schema_version"),
        )

    @classmethod
    def load(cls, path: str | Path) -> ChainCalibration:
        return cls.from_dict(load_json(path))

    def save(self, path: str | Path) -> None:
        dump_json(path, self.to_dict())


def subcarrier_frequencies_hz(
    sample_count: int,
    channel_frequency_mhz: float,
    subcarrier_spacing_hz: float = 312_500.0,
) -> np.ndarray:
    if type(sample_count) is not int or sample_count <= 0:
        raise ValueError("sample count must be a positive integer")
    if (
        type(channel_frequency_mhz) not in {int, float}
        or type(subcarrier_spacing_hz) not in {int, float}
        or not np.isfinite(channel_frequency_mhz)
        or not np.isfinite(subcarrier_spacing_hz)
        or channel_frequency_mhz <= 0
        or subcarrier_spacing_hz <= 0
    ):
        raise ValueError("sample count and subcarrier spacing must be positive")
    # Canonical even-N FFT-bin order is -N/2 .. N/2-1.  Using (N-1)/2
    # introduces a half-subcarrier bias into every steering vector.
    offsets = np.arange(sample_count, dtype=float) - sample_count // 2
    return channel_frequency_mhz * 1e6 + offsets * subcarrier_spacing_hz


def expected_pair_ratio(
    angle_deg: float,
    mapping: ChainMapping,
    frequencies_hz: np.ndarray,
) -> np.ndarray:
    """Ideal target/reference ratio using the *directed* physical baseline.

    Bearings use x=right/east, y=up/north and increase counter-clockwise.
    ``angle_deg`` is relative to mapping broadside.  Swapping target/reference
    or antenna coordinates therefore changes the steering sign explicitly.
    """

    if type(angle_deg) not in {int, float} or not np.isfinite(angle_deg):
        raise ValueError("steering angle must be finite")
    frequencies_hz = np.asarray(frequencies_hz, dtype=float)
    if (
        frequencies_hz.ndim != 1
        or frequencies_hz.size == 0
        or not np.all(np.isfinite(frequencies_hz))
    ):
        raise ValueError("steering frequencies must be a finite vector")
    bearing = np.deg2rad(mapping.array_broadside_heading_deg + angle_deg)
    arrival_unit = np.asarray([np.cos(bearing), np.sin(bearing)])
    path_projection_m = float(np.dot(mapping.baseline_vector_m, arrival_unit))
    phase = -2.0 * np.pi * path_projection_m * frequencies_hz / SPEED_OF_LIGHT_M_S
    return np.exp(1j * phase)


def _validate_cross_angle_steering_separation(
    mapping: ChainMapping,
    radio_config: RadioToneConfig,
    primary_angle_deg: float,
    validation_angle_deg: float,
) -> None:
    """Reject geometries whose two holdout angles are phase-indistinguishable."""

    frequencies = subcarrier_frequencies_hz(
        radio_config.sample_count,
        radio_config.channel_frequency_mhz,
        radio_config.subcarrier_spacing_hz,
    )
    primary = expected_pair_ratio(primary_angle_deg, mapping, frequencies)
    validation = expected_pair_ratio(validation_angle_deg, mapping, frequencies)
    wrapped_separation = np.abs(np.angle(primary * np.conjugate(validation)))
    median_separation = float(np.median(wrapped_separation))
    if median_separation < MIN_CROSS_ANGLE_STEERING_SEPARATION_RAD:
        raise ValueError(
            "antenna geometry cannot phase-discriminate the two calibration angles"
        )


def _csi_content_fingerprint(record: CSIRecord) -> tuple[object, ...]:
    """CSI payload identity, deliberately excluding forgeable header metadata."""

    samples = record.samples
    return samples.dtype.str, samples.shape, samples.tobytes(order="C")


def _valid_pair_ratios(
    groups: Iterable[GroupedPPDU],
    mapping: ChainMapping,
    *,
    allow_low_confidence_identity: bool,
    minimum_valid_tone_fraction: float,
) -> tuple[np.ndarray, list[GroupedPPDU], list]:
    ratios: list[np.ndarray] = []
    used: list[GroupedPPDU] = []
    records: list = []
    sample_count: int | None = None
    for group in groups:
        if group.hard_failure_flags:
            raise ValueError(
                "capture contains a hard-failed PPDU: "
                + ", ".join(group.hard_failure_flags)
            )
        try:
            reference, target = group.pair(
                mapping.reference_rx_idx,
                mapping.target_rx_idx,
                tx_idx=mapping.tx_idx,
                transport_stream=mapping.transport_stream,
                require_transport_stream_absent=(
                    mapping.require_transport_stream_absent
                ),
                allow_low_confidence_identity=allow_low_confidence_identity,
            )
        except KeyError:
            continue
        except ValueError as exc:
            # Strict calibration can skip weak identity, but ambiguity and hard
            # metadata failures must never be silently selected.
            if not allow_low_confidence_identity and "same-PPDU identity" in str(exc):
                continue
            raise
        if sample_count is None:
            sample_count = reference.sample_count
        if reference.sample_count != sample_count:
            raise ValueError("one inference window contains multiple tone counts")
        valid = (np.abs(reference.samples) > 1e-9) & (np.abs(target.samples) > 1e-9)
        fraction = float(valid.mean())
        if fraction < minimum_valid_tone_fraction:
            raise ValueError(
                f"paired packet has only {fraction:.3f} valid tones; "
                f"minimum is {minimum_valid_tone_fraction:.3f}"
            )
        ratio = np.full(reference.samples.shape, np.nan + 1j * np.nan)
        ratio[valid] = target.samples[valid] / reference.samples[valid]
        ratios.append(ratio)
        used.append(group)
        records.extend((reference, target))
    if not ratios:
        raise ValueError("no usable receive-chain pairs")
    return np.vstack(ratios), used, records


def estimate_chain_calibration(
    groups: Iterable[GroupedPPDU],
    mapping: ChainMapping,
    *,
    session_manifest: SessionManifest,
    known_angle_deg: float,
    validation_groups: Iterable[GroupedPPDU],
    validation_session_manifest: SessionManifest,
    validation_angle_deg: float,
    capture_path: str | Path | None = None,
    validation_capture_path: str | Path | None = None,
    minimum_packets: int = 30,
    minimum_valid_tone_fraction: float = 0.75,
    synthetic: bool = False,
) -> ChainCalibration:
    """Estimate hardware response from two opposite-side authorized captures."""

    if type(minimum_packets) is not int or minimum_packets < 2:
        raise ValueError("minimum_packets must be an integer of at least two")
    if (
        type(minimum_valid_tone_fraction) not in {int, float}
        or not np.isfinite(minimum_valid_tone_fraction)
        or not 0 < minimum_valid_tone_fraction <= 1
    ):
        raise ValueError("minimum_valid_tone_fraction must be in (0, 1]")
    if type(synthetic) is not bool:
        raise ValueError("synthetic must be boolean")

    if (
        mapping.receiver_id
        not in {
            session_manifest.receiver_id,
            validation_session_manifest.receiver_id,
        }
        or session_manifest.receiver_id != validation_session_manifest.receiver_id
    ):
        raise ValueError("mapping and both sessions must use one receiver_id")
    _validate_calibration_angles(known_angle_deg, validation_angle_deg)
    if (
        session_manifest.synthetic != synthetic
        or validation_session_manifest.synthetic != synthetic
    ):
        raise ValueError("synthetic mode must match both calibration session manifests")
    if (
        session_manifest.session_id == validation_session_manifest.session_id
        or session_manifest.capture_sha256 == validation_session_manifest.capture_sha256
        or session_manifest.computed_artifact_id()
        == validation_session_manifest.computed_artifact_id()
    ):
        raise ValueError(
            "calibration and opposite-side validation must be independent captures"
        )
    session_manifest.assert_radio_epoch_compatible(validation_session_manifest)
    if not synthetic and windows_overlap(
        session_manifest.time_window_ns,
        validation_session_manifest.time_window_ns,
    ):
        raise ValueError(
            "real opposite-side calibration capture windows must not overlap"
        )
    _validate_cross_angle_steering_separation(
        mapping,
        session_manifest.radio_config,
        known_angle_deg,
        validation_angle_deg,
    )
    ratios, used, records = _valid_pair_ratios(
        groups,
        mapping,
        allow_low_confidence_identity=False,
        minimum_valid_tone_fraction=minimum_valid_tone_fraction,
    )
    session_manifest.assert_records_verified(records, capture_path=capture_path)
    validation_ratios, validation_used, validation_records = _valid_pair_ratios(
        validation_groups,
        mapping,
        allow_low_confidence_identity=False,
        minimum_valid_tone_fraction=minimum_valid_tone_fraction,
    )
    validation_session_manifest.assert_records_verified(
        validation_records,
        capture_path=validation_capture_path,
    )
    if {_csi_content_fingerprint(record) for record in records} & {
        _csi_content_fingerprint(record) for record in validation_records
    }:
        raise ValueError("calibration and validation reuse identical CSI records")
    if len(used) < minimum_packets or len(validation_used) < minimum_packets:
        raise ValueError(
            f"each calibration angle needs at least {minimum_packets} strict pairs; "
            f"got {len(used)} and {len(validation_used)}"
        )
    if ratios.shape[1] != validation_ratios.shape[1]:
        raise ValueError("calibration captures have different tone counts")
    spacing = session_manifest.radio_config.subcarrier_spacing_hz
    frequencies = subcarrier_frequencies_hz(
        ratios.shape[1],
        session_manifest.radio_config.channel_frequency_mhz,
        spacing,
    )
    primary_physical = expected_pair_ratio(known_angle_deg, mapping, frequencies)
    validation_physical = expected_pair_ratio(
        validation_angle_deg, mapping, frequencies
    )
    primary_hardware = ratios / primary_physical[np.newaxis, :]
    validation_hardware = validation_ratios / validation_physical[np.newaxis, :]

    def circular_summary(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        unit_values = values / np.maximum(np.abs(values), 1e-12)
        circular_values = np.nanmean(unit_values, axis=0)
        return circular_values, np.abs(circular_values)

    primary_circular, primary_concentration = circular_summary(primary_hardware)
    validation_circular, validation_concentration = circular_summary(
        validation_hardware
    )
    if (
        float(np.median(primary_concentration)) < MIN_PER_CAPTURE_MEDIAN_CONCENTRATION
        or float(np.median(validation_concentration))
        < MIN_PER_CAPTURE_MEDIAN_CONCENTRATION
    ):
        raise ValueError("one calibration angle lacks stable inter-chain phase")
    cross_residual = np.abs(
        np.angle(primary_circular * np.conjugate(validation_circular))
    )
    residual_median = float(np.median(cross_residual))
    residual_p90 = float(np.percentile(cross_residual, 90))
    if (
        residual_median > MAX_CROSS_ANGLE_MEDIAN_RESIDUAL_RAD
        or residual_p90 > MAX_CROSS_ANGLE_P90_RESIDUAL_RAD
    ):
        raise ValueError(
            "opposite-side calibration disagrees; chain direction, geometry, "
            "static multipath, or phase state is not validated"
        )

    # Keep the opposite-side capture as a true hold-out: it gates geometry and
    # phase-state consistency but is not pooled into the fitted hardware ratio.
    hardware_observations = primary_hardware
    unit = hardware_observations / np.maximum(np.abs(hardware_observations), 1e-12)
    circular = np.nanmean(unit, axis=0)
    concentration = np.abs(circular)
    phase = np.angle(circular)
    amplitude = np.nanmedian(np.abs(hardware_observations), axis=0)
    estimate = amplitude * np.exp(1j * phase)
    invalid = ~np.isfinite(estimate) | (np.abs(estimate) < 1e-12)
    if np.any(invalid):
        raise ValueError("some tones lack valid calibration observations")

    created_utc = (
        "2000-01-01T00:00:00+00:00"
        if synthetic
        else datetime.now(timezone.utc).isoformat()
    )
    provisional = ChainCalibration.__new__(ChainCalibration)
    provisional.mapping = mapping
    provisional.session_manifest = session_manifest
    provisional.validation_session_manifest = validation_session_manifest
    provisional.subcarrier_spacing_hz = spacing
    provisional.complex_ratio = estimate
    provisional.phase_concentration = concentration
    provisional.known_angle_deg = known_angle_deg
    provisional.validation_angle_deg = validation_angle_deg
    provisional.packet_count = len(used)
    provisional.validation_packet_count = len(validation_used)
    provisional.cross_angle_phase_residual_median_rad = residual_median
    provisional.cross_angle_phase_residual_p90_rad = residual_p90
    provisional.minimum_valid_tone_fraction = minimum_valid_tone_fraction
    provisional.created_utc = created_utc
    provisional.synthetic = synthetic
    provisional.schema_version = 3
    calibration_id = provisional.computed_artifact_id()
    return ChainCalibration(
        calibration_id=calibration_id,
        mapping=mapping,
        session_manifest=session_manifest,
        validation_session_manifest=validation_session_manifest,
        subcarrier_spacing_hz=spacing,
        complex_ratio=estimate,
        phase_concentration=concentration,
        known_angle_deg=known_angle_deg,
        validation_angle_deg=validation_angle_deg,
        packet_count=len(used),
        validation_packet_count=len(validation_used),
        cross_angle_phase_residual_median_rad=residual_median,
        cross_angle_phase_residual_p90_rad=residual_p90,
        minimum_valid_tone_fraction=minimum_valid_tone_fraction,
        created_utc=created_utc,
        synthetic=synthetic,
    )


def calibrated_pair_ratios(
    groups: Iterable[GroupedPPDU],
    calibration: ChainCalibration,
    *,
    capture_manifest: SessionManifest,
    capture_path: str | Path | None = None,
    allow_low_confidence_identity: bool = False,
) -> tuple[np.ndarray, list[GroupedPPDU], list]:
    if calibration.calibration_id != calibration.computed_artifact_id():
        raise ValueError("calibration object no longer matches its artifact hash")
    if calibration.synthetic != capture_manifest.synthetic:
        raise ValueError("calibration and capture synthetic provenance differ")
    calibration.session_manifest.assert_radio_epoch_compatible(capture_manifest)
    ratios, used, records = _valid_pair_ratios(
        groups,
        calibration.mapping,
        allow_low_confidence_identity=allow_low_confidence_identity,
        minimum_valid_tone_fraction=calibration.minimum_valid_tone_fraction,
    )
    capture_manifest.assert_records_verified(records, capture_path=capture_path)
    if ratios.shape[1] != calibration.sample_count:
        raise ValueError("capture and calibration CSI lengths differ")
    return ratios / calibration.complex_ratio[np.newaxis, :], used, records
