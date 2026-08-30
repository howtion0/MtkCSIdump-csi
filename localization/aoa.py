"""Two-element Bartlett-style coarse angle support."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .calibration import (
    SPEED_OF_LIGHT_M_S,
    ChainCalibration,
    calibrated_pair_ratios,
    expected_pair_ratio,
    subcarrier_frequencies_hz,
)
from .contracts import RadioToneConfig
from .csi2 import quality_flag_names
from .grouping import GroupedPPDU
from .models import Evidence, normalized_entropy, softmax
from .session import SessionManifest


@dataclass(frozen=True)
class AliasCandidate:
    angle_deg: float
    wrap_order: int
    front_bearing_deg: float
    back_bearing_deg: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "angle_deg": self.angle_deg,
            "wrap_order": self.wrap_order,
            "front_bearing_deg": self.front_bearing_deg,
            "back_bearing_deg": self.back_bearing_deg,
        }


@dataclass(frozen=True)
class SectorSupport:
    label: str
    lower_deg: float
    upper_deg: float
    normalized_support: float

    def to_dict(self) -> dict[str, float | str]:
        return {
            "label": self.label,
            "lower_deg": self.lower_deg,
            "upper_deg": self.upper_deg,
            "normalized_support": self.normalized_support,
        }


@dataclass(frozen=True)
class AoAProvenance:
    calibration_id: str
    capture_manifest_id: str
    receiver_id: str
    transmitter_address: str
    start_host_timestamp_ns: int
    end_host_timestamp_ns: int
    timebase_id: str
    clock_uncertainty_ns: int
    radio_config: RadioToneConfig
    broadside_heading_deg: float
    boot_id: str
    radio_epoch: str
    driver_commit: str
    source_tree_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_id": self.calibration_id,
            "capture_manifest_id": self.capture_manifest_id,
            "receiver_id": self.receiver_id,
            "transmitter_address": self.transmitter_address,
            "actual_used_window": {
                "start_host_timestamp_ns": self.start_host_timestamp_ns,
                "end_host_timestamp_ns": self.end_host_timestamp_ns,
            },
            "timebase": {
                "id": self.timebase_id,
                "maximum_uncertainty_ns": self.clock_uncertainty_ns,
            },
            "radio_config": self.radio_config.to_dict(),
            "broadside_heading_deg": self.broadside_heading_deg,
            "boot_id": self.boot_id,
            "radio_epoch": self.radio_epoch,
            "driver_commit": self.driver_commit,
            "source_tree_hash": self.source_tree_hash,
        }


@dataclass
class AoAEstimate:
    angle_grid_deg: np.ndarray
    normalized_support: np.ndarray
    peak_angle_deg: float
    sectors: tuple[SectorSupport, ...]
    alias_candidates: tuple[AliasCandidate, ...]
    evidence: Evidence
    packet_count: int
    provenance: AoAProvenance

    def to_dict(self, *, include_spectrum: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "quantity": "coarse_two_element_aoa_normalized_support",
            "peak_angle_deg": self.peak_angle_deg,
            "packet_count": self.packet_count,
            "provenance": self.provenance.to_dict(),
            "sectors": [sector.to_dict() for sector in self.sectors],
            "alias_candidates": [item.to_dict() for item in self.alias_candidates],
            "evidence": {
                "score": self.evidence.score,
                "flags": list(self.evidence.flags),
                "details": self.evidence.details,
            },
        }
        if include_spectrum:
            result["spectrum"] = [
                {"angle_deg": float(angle), "normalized_support": float(support)}
                for angle, support in zip(self.angle_grid_deg, self.normalized_support)
            ]
        return result


def enumerate_grating_lobe_candidates(
    wrapped_phase_rad: float,
    baseline_vector_m: np.ndarray | tuple[float, float],
    center_frequency_hz: float,
    *,
    broadside_heading_deg: float = 0.0,
) -> tuple[AliasCandidate, ...]:
    """Enumerate all visible angles consistent with one wrapped phase."""

    baseline = np.asarray(baseline_vector_m, dtype=float)
    if baseline.shape != (2,) or not np.all(np.isfinite(baseline)):
        raise ValueError("directed baseline must contain two finite coordinates")
    spacing_m = float(np.linalg.norm(baseline))
    if spacing_m <= 0 or center_frequency_hz <= 0:
        raise ValueError("baseline spacing and center frequency must be positive")
    wavelength = SPEED_OF_LIGHT_M_S / center_frequency_hz
    max_wrap = int(np.ceil(spacing_m / wavelength)) + 2
    baseline_heading_deg = float(np.rad2deg(np.arctan2(baseline[1], baseline[0])))
    values: list[AliasCandidate] = []
    for wrap_order in range(-max_wrap, max_wrap + 1):
        projection_m = (
            -(wrapped_phase_rad + 2.0 * np.pi * wrap_order) * wavelength / (2.0 * np.pi)
        )
        cosine = projection_m / spacing_m
        if not -1.0 - 1e-12 <= cosine <= 1.0 + 1e-12:
            continue
        offset_deg = float(np.rad2deg(np.arccos(np.clip(cosine, -1.0, 1.0))))
        for bearing_deg in (
            baseline_heading_deg + offset_deg,
            baseline_heading_deg - offset_deg,
        ):
            angle = (bearing_deg - broadside_heading_deg + 180.0) % 360.0 - 180.0
            if not -90.0 - 1e-9 <= angle <= 90.0 + 1e-9:
                continue
            front = (broadside_heading_deg + angle) % 360.0
            back = (2.0 * baseline_heading_deg - front) % 360.0
            if not any(abs(existing.angle_deg - angle) < 1e-6 for existing in values):
                values.append(AliasCandidate(float(angle), wrap_order, front, back))
    return tuple(sorted(values, key=lambda value: value.angle_deg))


def _sector_support(
    angles: np.ndarray, support: np.ndarray, sector_count: int
) -> tuple[SectorSupport, ...]:
    if sector_count < 13:
        raise ValueError("at least 13 sectors are required for the coarse UI")
    edges = np.linspace(-90.0, 90.0, sector_count + 1)
    raw: list[float] = []
    for index in range(sector_count):
        upper_operator = (
            angles <= edges[index + 1]
            if index == sector_count - 1
            else angles < edges[index + 1]
        )
        mask = (angles >= edges[index]) & upper_operator
        raw.append(float(support[mask].sum()))
    total = sum(raw) or 1.0
    return tuple(
        SectorSupport(
            label=f"S{index + 1:02d} {edges[index]:+.1f}..{edges[index + 1]:+.1f} deg",
            lower_deg=float(edges[index]),
            upper_deg=float(edges[index + 1]),
            normalized_support=value / total,
        )
        for index, value in enumerate(raw)
    )


def estimate_coarse_aoa(
    groups: Iterable[GroupedPPDU],
    calibration: ChainCalibration,
    *,
    capture_manifest: SessionManifest,
    capture_path: str | Path | None = None,
    sector_count: int = 13,
    angle_step_deg: float = 0.5,
    allow_low_confidence_pairing: bool = False,
    minimum_packets: int = 12,
) -> AoAEstimate:
    """Return normalized display support, not calibrated statistical inference."""

    if not 0 < angle_step_deg <= 5:
        raise ValueError("angle_step_deg must be in (0, 5]")
    if minimum_packets < 2:
        raise ValueError("minimum_packets must be at least two")
    ratios, used, used_records = calibrated_pair_ratios(
        groups,
        calibration,
        capture_manifest=capture_manifest,
        capture_path=capture_path,
        allow_low_confidence_identity=allow_low_confidence_pairing,
    )
    if len(used) < minimum_packets:
        raise ValueError(
            f"AoA needs at least {minimum_packets} paired packets; got {len(used)}"
        )
    valid = (
        np.isfinite(ratios.real) & np.isfinite(ratios.imag) & (np.abs(ratios) > 1e-12)
    )
    unit = np.zeros(ratios.shape, dtype=np.complex128)
    unit[valid] = ratios[valid] / np.abs(ratios[valid])
    tone_weights = np.clip(calibration.phase_concentration, 0.0, 1.0)
    tone_weights *= valid.sum(axis=0) / max(ratios.shape[0], 1)
    valid_tone_count = int(np.count_nonzero(tone_weights > 0))
    required_tones = int(
        np.ceil(calibration.sample_count * calibration.minimum_valid_tone_fraction)
    )
    if valid_tone_count < required_tones or float(tone_weights.sum()) <= 0:
        raise ValueError("too few calibrated tones remain usable")

    angles = np.arange(-90.0, 90.0 + angle_step_deg / 2.0, angle_step_deg)
    frequencies = subcarrier_frequencies_hz(
        calibration.sample_count,
        calibration.channel_frequency_mhz,
        calibration.subcarrier_spacing_hz,
    )
    spectrum = np.zeros(angles.shape, dtype=float)
    weights = valid * tone_weights[np.newaxis, :]
    for index, angle in enumerate(angles):
        expected = expected_pair_ratio(float(angle), calibration.mapping, frequencies)
        alignment = unit * np.conjugate(expected[np.newaxis, :])
        element_response = np.abs(1.0 + alignment) ** 2 / 4.0
        spectrum[index] = float(
            np.sum(element_response * weights) / max(float(weights.sum()), 1e-12)
        )

    scale = max(float(np.percentile(spectrum, 90) - np.percentile(spectrum, 10)), 1e-6)
    support = softmax((spectrum - np.median(spectrum)) / scale, temperature=0.65)
    peak_index = int(np.argmax(support))
    peak_angle = float(angles[peak_index])
    sectors = _sector_support(angles, support, sector_count)

    usable_indices = np.flatnonzero(valid.sum(axis=0) > 0)
    center_index = int(
        usable_indices[
            np.argmin(
                np.abs(
                    frequencies[usable_indices]
                    - calibration.channel_frequency_mhz * 1e6
                )
            )
        ]
    )
    center_values = unit[:, center_index]
    center_values = center_values[np.abs(center_values) > 0]
    wrapped_phase = float(np.angle(np.mean(center_values)))
    if not np.isfinite(wrapped_phase):
        raise ValueError("no finite phase remains for alias enumeration")
    aliases = enumerate_grating_lobe_candidates(
        wrapped_phase,
        calibration.mapping.baseline_vector_m,
        float(frequencies[center_index]),
        broadside_heading_deg=calibration.mapping.array_broadside_heading_deg,
    )

    valid_counts = valid.sum(axis=0)
    per_tone_coherence = np.zeros(calibration.sample_count, dtype=float)
    usable_tones = valid_counts > 0
    per_tone_coherence[usable_tones] = (
        np.abs(unit[:, usable_tones].sum(axis=0)) / valid_counts[usable_tones]
    )
    coherence = float(
        np.average(per_tone_coherence, weights=np.maximum(tone_weights, 1e-9))
    )
    entropy = normalized_entropy(support)
    peak_prominence = float(
        np.clip(
            (spectrum[peak_index] - np.median(spectrum))
            / max(spectrum[peak_index], 1e-9),
            0,
            1,
        )
    )
    pairing_quality = float(np.mean([group.evidence.score for group in used]))
    calibration_quality = float(np.median(calibration.phase_concentration))
    sample_size_factor = min(1.0, np.sqrt(len(used) / max(minimum_packets * 2, 1)))
    quality = float(
        np.clip(
            (
                0.30 * coherence
                + 0.20 * (1.0 - entropy)
                + 0.15 * peak_prominence
                + 0.20 * pairing_quality
                + 0.15 * calibration_quality
            )
            * sample_size_factor,
            0.0,
            1.0,
        )
    )

    flags = ["single_baseline", "front_back_ambiguous", "coarse_sector_only"]
    if calibration.mapping.require_transport_stream_absent:
        flags.append("transport_stream_metadata_explicitly_absent")
    center_wavelength = SPEED_OF_LIGHT_M_S / (calibration.channel_frequency_mhz * 1e6)
    if calibration.mapping.spacing_m > center_wavelength / 2.0:
        flags.append("grating_lobes_possible")
        quality *= 0.72
    if len(aliases) > 1:
        flags.append("multiple_wrapped_angle_candidates")
    if any(group.identity_mode != "strict" for group in used):
        flags.extend(("low_confidence_identity_used", "phase_result_experimental"))
        quality *= 0.55
    if len(used) < minimum_packets * 2:
        flags.append("limited_packet_sample")
    if calibration.synthetic:
        flags.append("synthetic_calibration")
    for group in used:
        flags.extend(group.evidence.flags)

    quality_flags_union = int(
        np.bitwise_or.reduce(
            [
                record.quality_flags
                for group in used
                for record in group.records_by_stream.values()
            ]
        )
    )
    evidence = Evidence(
        score=float(np.clip(quality, 0.0, 1.0)),
        flags=tuple(dict.fromkeys(flags)),
        details={
            "pairing_quality": pairing_quality,
            "phase_coherence": coherence,
            "calibration_concentration": calibration_quality,
            "support_entropy": entropy,
            "packet_sample_factor": sample_size_factor,
            "valid_tone_count": valid_tone_count,
            "antenna_spacing_m": calibration.mapping.spacing_m,
            "center_wavelength_m": center_wavelength,
            "quality_flags_union": quality_flags_union,
            "quality_flag_names": list(quality_flag_names(quality_flags_union)),
            "interpretation": (
                "normalized support and evidence quality, not calibrated correctness probability"
            ),
        },
    )
    used_transmitters = {record.transmitter_address for record in used_records}
    if len(used_transmitters) != 1:
        raise ValueError("AoA used records do not have one transmitter")
    provenance = AoAProvenance(
        calibration_id=calibration.calibration_id,
        capture_manifest_id=capture_manifest.computed_artifact_id(),
        receiver_id=capture_manifest.receiver_id,
        transmitter_address=next(iter(used_transmitters)),
        start_host_timestamp_ns=min(
            record.host_timestamp_ns for record in used_records
        ),
        end_host_timestamp_ns=max(record.host_timestamp_ns for record in used_records),
        timebase_id=capture_manifest.timebase_id,
        clock_uncertainty_ns=capture_manifest.clock_uncertainty_ns,
        radio_config=capture_manifest.radio_config,
        broadside_heading_deg=calibration.mapping.array_broadside_heading_deg,
        boot_id=capture_manifest.boot_id,
        radio_epoch=capture_manifest.radio_epoch,
        driver_commit=capture_manifest.driver_commit,
        source_tree_hash=capture_manifest.source_tree_hash,
    )
    return AoAEstimate(
        angle_grid_deg=angles,
        normalized_support=support,
        peak_angle_deg=peak_angle,
        sectors=sectors,
        alias_candidates=aliases,
        evidence=evidence,
        packet_count=len(used),
        provenance=provenance,
    )
