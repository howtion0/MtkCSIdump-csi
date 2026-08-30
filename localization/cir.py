"""Relative channel-impulse-response and delay-spread diagnostics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .contracts import validate_analysis_record
from .models import CSIRecord, Evidence
from .session import SessionManifest


@dataclass
class CIRDiagnostics:
    relative_delay_ns: np.ndarray
    normalized_power: np.ndarray
    strongest_capture_bin: int
    rms_delay_spread_ns: float
    secondary_peak_delay_ns: float | None
    evidence: Evidence

    def to_dict(self, *, include_profile: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "quantity": "relative_cir_diagnostics",
            "strongest_capture_bin": self.strongest_capture_bin,
            "rms_delay_spread_ns": self.rms_delay_spread_ns,
            "secondary_peak_delay_ns": self.secondary_peak_delay_ns,
            "evidence": {
                "score": self.evidence.score,
                "flags": list(self.evidence.flags),
                "details": self.evidence.details,
            },
        }
        if include_profile:
            result["profile"] = [
                {"relative_delay_ns": float(delay), "power": float(power)}
                for delay, power in zip(self.relative_delay_ns, self.normalized_power)
            ]
        return result


def _frequency_response_matrix(csi: np.ndarray | Iterable[np.ndarray]) -> np.ndarray:
    array = np.asarray(csi, dtype=np.complex128)
    if array.ndim == 1:
        array = array[np.newaxis, :]
    if array.ndim != 2 or array.shape[1] == 0:
        raise ValueError("CSI must be one vector or a packet-by-subcarrier matrix")
    if not np.all(np.isfinite(array.real)) or not np.all(np.isfinite(array.imag)):
        raise ValueError("CSI contains non-finite values")
    return array


def relative_cir_diagnostics(
    csi: np.ndarray | Iterable[np.ndarray],
    *,
    subcarrier_spacing_hz: float = 312_500.0,
    subcarrier_indices: np.ndarray | None = None,
    fft_size: int | None = None,
    zero_pad_factor: int = 8,
) -> CIRDiagnostics:
    """Compute a peak-aligned relative delay profile.

    Absolute delay is deliberately removed: packet-detection delay, sampling
    offset, oscillator effects and unknown phase reference make fixed-channel
    CSI unsuitable for absolute ToF range.  Returned delays are relative to the
    strongest captured path-like peak.
    """

    if subcarrier_spacing_hz <= 0:
        raise ValueError("subcarrier_spacing_hz must be positive")
    if zero_pad_factor < 1:
        raise ValueError("zero_pad_factor must be at least one")
    responses = _frequency_response_matrix(csi)
    tone_count = responses.shape[1]
    flags = [
        "relative_delay_only",
        "not_absolute_tof",
        "not_absolute_range",
        "per_packet_peak_aligned_noncoherent_power",
    ]

    if subcarrier_indices is None:
        base_size = tone_count if fft_size is None else int(fft_size)
        if base_size < tone_count:
            raise ValueError("fft_size cannot be smaller than the CSI vector")
        centered = np.zeros((responses.shape[0], base_size), dtype=np.complex128)
        start = (base_size - tone_count) // 2
        centered[:, start : start + tone_count] = responses
        if tone_count != base_size:
            flags.append("unknown_bins_zero_filled")
        flags.append("assumed_contiguous_centered_subcarriers")
        if tone_count not in (64, 128, 256, 512):
            flags.append("nonstandard_sample_count_requires_tone_map")
    else:
        indices = np.asarray(subcarrier_indices, dtype=int)
        if indices.shape != (tone_count,):
            raise ValueError("subcarrier_indices shape must match CSI")
        if len(np.unique(indices)) != indices.size:
            raise ValueError("subcarrier_indices contains duplicates")
        if fft_size is None:
            base_size = max(2, 2 * int(np.max(np.abs(indices))) + 2)
            base_size = 1 << int(np.ceil(np.log2(base_size)))
        else:
            base_size = int(fft_size)
        if base_size <= 0 or np.any(np.abs(indices) >= base_size):
            raise ValueError("subcarrier index is outside fft_size")
        standard = np.zeros((responses.shape[0], base_size), dtype=np.complex128)
        standard[:, np.mod(indices, base_size)] = responses
        centered = np.fft.fftshift(standard, axes=1)
        if indices.size < base_size:
            flags.append("unreported_bins_zero_filled")

    nfft = base_size * int(zero_pad_factor)
    padded_centered = np.zeros((responses.shape[0], nfft), dtype=np.complex128)
    start = (nfft - base_size) // 2
    padded_centered[:, start : start + base_size] = centered
    impulse = np.fft.ifft(np.fft.ifftshift(padded_centered, axes=1), axis=1)
    power = np.abs(impulse) ** 2
    strongest_bins = np.argmax(power, axis=1)
    strongest = int(np.median(strongest_bins))

    aligned_rows = np.stack(
        [np.roll(row, -int(peak)) for row, peak in zip(power, strongest_bins)]
    )
    # After peak alignment only the first half is interpreted as non-negative
    # excess delay; the second half is the cyclic IFFT image.
    useful_count = nfft // 2
    aligned_rows = aligned_rows[:, :useful_count]
    totals = aligned_rows.sum(axis=1)
    if np.any(totals <= 0):
        raise ValueError("CIR power is zero")
    normalized = np.mean(aligned_rows / totals[:, np.newaxis], axis=0)
    normalized /= normalized.sum()
    delay_s = np.arange(useful_count, dtype=float) / (nfft * subcarrier_spacing_hz)
    mean_delay = float(np.sum(normalized * delay_s))
    rms_s = float(np.sqrt(np.sum(normalized * (delay_s - mean_delay) ** 2)))

    guard = max(2, zero_pad_factor)
    secondary_delay: float | None = None
    if normalized.size > guard:
        secondary_index = guard + int(np.argmax(normalized[guard:]))
        if normalized[secondary_index] >= normalized[0] * 0.05:
            secondary_delay = float(delay_s[secondary_index] * 1e9)

    occupied_bandwidth_hz = base_size * subcarrier_spacing_hz
    resolution_ns = 1e9 / occupied_bandwidth_hz
    evidence_score = 0.72 if subcarrier_indices is not None else 0.50
    if "nonstandard_sample_count_requires_tone_map" in flags:
        evidence_score *= 0.65
    evidence = Evidence(
        score=evidence_score,
        flags=tuple(flags),
        details={
            "ifft_size": nfft,
            "base_fft_size": base_size,
            "subcarrier_spacing_hz": subcarrier_spacing_hz,
            "nominal_delay_resolution_ns": resolution_ns,
            "zero_padding_is_interpolation_not_new_resolution": True,
            "packet_count": int(responses.shape[0]),
            "strongest_bin_spread": int(
                np.max(strongest_bins) - np.min(strongest_bins)
            ),
        },
    )
    return CIRDiagnostics(
        relative_delay_ns=delay_s * 1e9,
        normalized_power=normalized,
        strongest_capture_bin=strongest,
        rms_delay_spread_ns=rms_s * 1e9,
        secondary_peak_delay_ns=secondary_delay,
        evidence=evidence,
    )


def relative_cir_from_records(
    records: Iterable[CSIRecord],
    *,
    session_manifest: SessionManifest,
    capture_path: str | Path | None = None,
    rx_idx: int,
    tx_idx: int | None = None,
    transport_stream: int | None = None,
    minimum_packets: int = 12,
    subcarrier_spacing_hz: float | None = None,
    zero_pad_factor: int = 8,
) -> CIRDiagnostics:
    """Apply the Stage-2 tone/provenance contract before CIR diagnostics."""

    materialized = list(records)
    session_manifest.assert_records_verified(materialized, capture_path=capture_path)
    selected = [
        record
        for record in materialized
        if record.rx_idx == rx_idx
        and (tx_idx is None or record.tx_idx == tx_idx)
        and (transport_stream is None or record.transport_stream == transport_stream)
    ]
    contexts = {(record.tx_idx, record.transport_stream) for record in selected}
    if not selected:
        raise ValueError("requested RX/Tx/stream has no CSI records")
    if len(contexts) != 1:
        raise ValueError("ambiguous Tx/transport stream; select both explicitly")
    if len(selected) < minimum_packets:
        raise ValueError(
            f"relative CIR needs at least {minimum_packets} packets; got {len(selected)}"
        )
    signatures = {validate_analysis_record(record).signature() for record in selected}
    if signatures != {session_manifest.radio_config.signature()}:
        raise ValueError("CIR records have incompatible radio/tone metadata")
    contracted_spacing_hz = session_manifest.radio_config.subcarrier_spacing_hz
    if (
        subcarrier_spacing_hz is not None
        and subcarrier_spacing_hz != contracted_spacing_hz
    ):
        raise ValueError("requested spacing differs from rx_mode/tone-profile contract")
    result = relative_cir_diagnostics(
        np.vstack([record.samples for record in selected]),
        subcarrier_spacing_hz=contracted_spacing_hz,
        fft_size=session_manifest.radio_config.sample_count,
        zero_pad_factor=zero_pad_factor,
    )
    flags = tuple(
        dict.fromkeys(
            (
                *result.evidence.flags,
                "stage2_canonical_tone_order_validated",
                "sample_size_gate_passed",
            )
        )
    )
    sample_factor = min(1.0, np.sqrt(len(selected) / (minimum_packets * 2)))
    result.evidence = Evidence(
        score=float(np.clip(result.evidence.score * sample_factor, 0.0, 1.0)),
        flags=flags,
        details={
            **result.evidence.details,
            "packet_count": len(selected),
            "packet_sample_factor": sample_factor,
            "receiver_id": session_manifest.receiver_id,
        },
    )
    return result
