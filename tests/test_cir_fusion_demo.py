from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from localization.cir import relative_cir_diagnostics, relative_cir_from_records
from localization.cli import main
from localization.contracts import (
    CANONICAL_TONE_MODE,
    RX_MODE_TONE_PROFILES,
    RadioToneConfig,
)
from localization.fusion import APObservation, fuse_grid_support
from localization.range_proxy import RangeFeatureProvenance
from localization.simulate import (
    SYNTHETIC_NOTICE,
    default_mapping,
    simulate_strict_groups,
    synthetic_session_manifest,
)


def test_relative_cir_finds_excess_delay_but_refuses_absolute_range() -> None:
    sample_count = 64
    spacing_hz = 312_500.0
    frequency = (np.arange(sample_count) - sample_count // 2) * spacing_hz
    echo_delay_s = 100e-9
    response = 1.0 + 0.55 * np.exp(-1j * 2.0 * np.pi * frequency * echo_delay_s)
    diagnostics = relative_cir_diagnostics(
        response,
        subcarrier_spacing_hz=spacing_hz,
        fft_size=64,
        zero_pad_factor=16,
    )
    assert diagnostics.secondary_peak_delay_ns == pytest.approx(100.0, abs=20.0)
    assert diagnostics.rms_delay_spread_ns > 0
    assert "relative_delay_only" in diagnostics.evidence.flags
    assert "not_absolute_range" in diagnostics.evidence.flags
    assert diagnostics.evidence.details[
        "zero_padding_is_interpolation_not_new_resolution"
    ]


def test_relative_cir_noncoherent_average_survives_packet_phase_flip() -> None:
    tone_count = 64
    spacing_hz = 312_500.0
    frequency = (np.arange(tone_count) - tone_count // 2) * spacing_hz
    response = 1.0 + 0.5 * np.exp(-1j * 2.0 * np.pi * frequency * 100e-9)
    result = relative_cir_diagnostics(
        np.vstack((response, -response)),
        subcarrier_spacing_hz=spacing_hz,
        fft_size=tone_count,
        zero_pad_factor=16,
    )
    assert result.normalized_power.sum() == pytest.approx(1.0)
    assert result.secondary_peak_delay_ns == pytest.approx(100.0, abs=20.0)
    assert "per_packet_peak_aligned_noncoherent_power" in result.evidence.flags


def test_cir_record_path_enforces_manifest_tones_selector_and_sample_floor() -> None:
    groups = simulate_strict_groups(
        angle_deg=12,
        packet_count=12,
        mapping=default_mapping(receiver_id="receiver-a"),
    )
    records = [
        record for group in groups for record in group.records_by_stream.values()
    ]
    manifest = synthetic_session_manifest(
        groups, receiver_id="receiver-a", session_id="cir"
    )
    result = relative_cir_from_records(
        records,
        session_manifest=manifest,
        rx_idx=0,
        tx_idx=0,
        transport_stream=0,
    )
    assert "stage2_canonical_tone_order_validated" in result.evidence.flags
    with pytest.raises(ValueError, match="at least 13 packets"):
        relative_cir_from_records(
            records,
            session_manifest=manifest,
            rx_idx=0,
            tx_idx=0,
            transport_stream=0,
            minimum_packets=13,
        )


def _radio() -> RadioToneConfig:
    profile, spacing = RX_MODE_TONE_PROFILES[4]
    return RadioToneConfig(
        band=1,
        channel_frequency_mhz=5500,
        channel_bw_enum=0,
        data_bw_enum=0,
        sample_count=64,
        rx_mode=4,
        subcarrier_spacing_hz=spacing,
        tone_mode=CANONICAL_TONE_MODE,
        tone_profile=profile,
        frequency_source="center",
    )


def _gaussian_angle(mean_deg: float, sigma_deg: float = 3.0):
    grid = np.linspace(-90.0, 90.0, 361)
    support = np.exp(-0.5 * ((grid - mean_deg) / sigma_deg) ** 2)
    return grid, support


def _observation(
    receiver_id: str,
    position: tuple[float, float],
    heading: float,
    angle: float,
    *,
    evidence: float = 0.9,
    start: int = 1_000_000,
    end: int = 4_000_000,
    timebase: str = "ptp-domain-a",
    uncertainty: int = 10_000,
    range_evidence: float = 0.9,
    range_flags: tuple[str, ...] = (),
) -> APObservation:
    grid, support = _gaussian_angle(angle)
    bands = {"near": (0.0, 1.5), "mid": (1.5, 3.5), "far": (3.5, float("inf"))}
    weights = (
        {"near": 0.0, "mid": 0.15, "far": 0.85}
        if receiver_id.endswith("A")
        else {"near": 0.0, "mid": 0.95, "far": 0.05}
    )
    digest = ("a" if receiver_id.endswith("A") else "b") * 64
    manifest_digest = ("c" if receiver_id.endswith("A") else "d") * 64
    model_digest = ("e" if receiver_id.endswith("A") else "f") * 64
    return APObservation(
        receiver_id=receiver_id,
        calibration_id="sha256:" + digest,
        capture_manifest_id="sha256:" + manifest_digest,
        transmitter_address="02:11:22:33:44:55",
        start_host_timestamp_ns=start,
        end_host_timestamp_ns=end,
        timebase_id=timebase,
        clock_uncertainty_ns=uncertainty,
        radio_config=_radio(),
        position_m=position,
        broadside_heading_deg=heading,
        angle_grid_deg=grid,
        angle_normalized_support=support,
        evidence_score=evidence,
        range_support_weights=weights,
        range_bands_m=bands,
        range_evidence_score=range_evidence,
        range_evidence_flags=range_flags,
        range_model_id="sha256:" + model_digest,
        range_device_id="02:11:22:33:44:55",
        range_room_id="lab-A",
        range_feature_provenance=RangeFeatureProvenance(
            capture_manifest_id="sha256:" + manifest_digest,
            receiver_id=receiver_id,
            transmitter_address="02:11:22:33:44:55",
            start_host_timestamp_ns=start,
            end_host_timestamp_ns=end,
            radio_config=_radio(),
        ),
    )


def test_two_receiver_fused_support_keeps_honest_names_and_geometry() -> None:
    observations = [
        _observation("receiver-A", (0.7, 0.7), 0.0, 34.1),
        _observation("receiver-B", (5.3, 0.8), 180.0, -53.1),
    ]
    result = fuse_grid_support(
        observations,
        room_bounds_m=(0.0, 6.0, 0.0, 4.0),
        room_id="lab-A",
        grid_step_m=0.05,
    )
    assert result.display_peak_position_m == pytest.approx((3.8, 2.8), abs=0.15)
    assert result.fused_normalized_support.sum() == pytest.approx(1)
    assert result.display_mass_radius_80_m < 0.6
    assert "multi_receiver_geometry" in result.evidence.flags
    assert "not_bayesian_inference" in result.evidence.flags
    serialized = json.dumps(result.to_dict(include_grid=True))
    assert "posterior" not in serialized
    assert "credible_radius" not in serialized
    assert '"probability"' not in serialized


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            {
                "receiver_id": "receiver-A",
                "range_feature_provenance": RangeFeatureProvenance(
                    capture_manifest_id="sha256:" + "d" * 64,
                    receiver_id="receiver-A",
                    transmitter_address="02:11:22:33:44:55",
                    start_host_timestamp_ns=1_000_000,
                    end_host_timestamp_ns=4_000_000,
                    radio_config=_radio(),
                ),
            },
            "receiver IDs",
        ),
        ({"position_m": (0.7, 0.7)}, "too close"),
        (
            {
                "transmitter_address": "02:11:22:33:44:66",
                "range_device_id": "02:11:22:33:44:66",
                "range_feature_provenance": RangeFeatureProvenance(
                    capture_manifest_id="sha256:" + "d" * 64,
                    receiver_id="receiver-B",
                    transmitter_address="02:11:22:33:44:66",
                    start_host_timestamp_ns=1_000_000,
                    end_host_timestamp_ns=4_000_000,
                    radio_config=_radio(),
                ),
            },
            "same transmitter",
        ),
        (
            {
                "start_host_timestamp_ns": 6_000_000,
                "end_host_timestamp_ns": 7_000_000,
                "range_feature_provenance": RangeFeatureProvenance(
                    capture_manifest_id="sha256:" + "d" * 64,
                    receiver_id="receiver-B",
                    transmitter_address="02:11:22:33:44:55",
                    start_host_timestamp_ns=6_000_000,
                    end_host_timestamp_ns=7_000_000,
                    radio_config=_radio(),
                ),
            },
            "lack overlap",
        ),
        ({"timebase_id": "unsynchronized-clock"}, "aligned timebase"),
        ({"clock_uncertainty_ns": 2_000_000}, "clock uncertainty"),
        (
            {
                "radio_config": replace(_radio(), channel_frequency_mhz=5520),
                "range_feature_provenance": RangeFeatureProvenance(
                    capture_manifest_id="sha256:" + "d" * 64,
                    receiver_id="receiver-B",
                    transmitter_address="02:11:22:33:44:55",
                    start_host_timestamp_ns=1_000_000,
                    end_host_timestamp_ns=4_000_000,
                    radio_config=replace(_radio(), channel_frequency_mhz=5520),
                ),
            },
            "incompatible",
        ),
    ],
)
def test_fusion_rejects_incompatible_multi_receiver_evidence(
    mutation: dict[str, object], message: str
) -> None:
    left = _observation("receiver-A", (0.7, 0.7), 0.0, 34.1)
    right = replace(_observation("receiver-B", (5.3, 0.8), 180.0, -53.1), **mutation)
    with pytest.raises(ValueError, match=message):
        fuse_grid_support(
            [left, right],
            room_bounds_m=(0.0, 6.0, 0.0, 4.0),
            room_id="lab-A",
        )


def test_low_evidence_receiver_is_ignored_without_weight_floor() -> None:
    accepted = _observation("receiver-A", (0.7, 0.7), 0.0, 34.1)
    low = _observation("receiver-B", (5.3, 0.8), 180.0, -53.1, evidence=0.05)
    result = fuse_grid_support(
        [accepted, low],
        room_bounds_m=(0.0, 6.0, 0.0, 4.0),
        room_id="lab-A",
    )
    assert "low_evidence_receivers_ignored" in result.evidence.flags
    assert result.evidence.details["receiver_count"] == 1
    with pytest.raises(ValueError, match="zero/invalid evidence"):
        replace(low, evidence_score=0.0)


def test_fusion_rejects_nonfinite_prior_and_thresholds() -> None:
    observation = _observation("receiver-A", (0.7, 0.7), 0.0, 34.1)
    with pytest.raises(ValueError, match="invalid values"):
        fuse_grid_support(
            [observation],
            room_bounds_m=(0.0, 1.0, 0.0, 1.0),
            room_id="lab-A",
            grid_step_m=0.5,
            prior_support=np.asarray(
                [[1.0, 1.0, 1.0], [1.0, np.nan, 1.0], [1.0, 1.0, 1.0]]
            ),
        )
    with pytest.raises(ValueError, match="thresholds"):
        fuse_grid_support(
            [observation],
            room_bounds_m=(0.0, 1.0, 0.0, 1.0),
            room_id="lab-A",
            minimum_evidence_score=float("nan"),
        )


def test_unseen_device_range_is_ignored_instead_of_steering_heatmap() -> None:
    left = _observation(
        "receiver-A",
        (0.7, 0.7),
        0.0,
        34.1,
        range_flags=("unseen_transmitter_device",),
    )
    right = _observation("receiver-B", (5.3, 0.8), 180.0, -53.1)
    result = fuse_grid_support(
        [left, right],
        room_bounds_m=(0.0, 6.0, 0.0, 4.0),
        room_id="lab-A",
    )
    assert "low_or_shifted_range_proxy_ignored" in result.evidence.flags
    assert "range:unseen_transmitter_device" in result.evidence.flags
    assert result.evidence.details["range_receiver_count_ignored"] == 1


def test_demo_outputs_prominently_labeled_reproducible_synthetic_artifacts(
    tmp_path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        assert main(["demo", "--output-dir", str(directory), "--sectors", "17"]) == 0
    filenames = (
        "synthetic_result.json",
        "synthetic_room.svg",
        "synthetic_calibration_A.json",
        "synthetic_calibration_B.json",
        "synthetic_session_A.json",
        "synthetic_session_B.json",
        "synthetic_range_proxy.json",
    )
    for filename in filenames:
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
        checked_in = Path(__file__).parents[1] / "synthetic-demo" / filename
        assert checked_in.read_bytes() == (first / filename).read_bytes()
    result = json.loads((first / "synthetic_result.json").read_text())
    svg = (first / "synthetic_room.svg").read_text()
    assert result["notice"] == SYNTHETIC_NOTICE
    for receiver_payload in result["per_receiver"].values():
        range_payload = receiver_payload["range_proxy"]
        aoa_payload = receiver_payload["aoa"]
        assert "capture_bound_feature_window" in range_payload["evidence"]["flags"]
        assert (
            range_payload["feature_provenance"]["capture_manifest_id"]
            == aoa_payload["provenance"]["capture_manifest_id"]
        )
    assert SYNTHETIC_NOTICE in svg
    assert "not_absolute_range" in result["fused_support"]["evidence"]["flags"]
