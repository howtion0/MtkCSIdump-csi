from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from localization.aoa import (
    enumerate_grating_lobe_candidates,
    estimate_coarse_aoa,
)
from localization.calibration import (
    AntennaElement,
    ChainCalibration,
    estimate_chain_calibration,
    expected_pair_ratio,
    subcarrier_frequencies_hz,
)
from localization.grouping import group_same_ppdu
from localization.simulate import (
    default_mapping,
    simulate_strict_groups,
    synthetic_hardware_ratio,
    synthetic_session_manifest,
)


def _calibration_and_target(
    *, angle_deg: float = 27.0, spacing_m: float = 0.028, packets: int = 40
):
    mapping = default_mapping(spacing_m=spacing_m, receiver_id="receiver-a")
    hardware = synthetic_hardware_ratio(64)
    calibration_groups = simulate_strict_groups(
        angle_deg=25,
        packet_count=36,
        mapping=mapping,
        hardware_ratio=hardware,
        seed=1,
    )
    calibration_session = synthetic_session_manifest(
        calibration_groups,
        receiver_id="receiver-a",
        session_id="calibration",
    )
    validation_groups = simulate_strict_groups(
        angle_deg=-25,
        packet_count=36,
        mapping=mapping,
        hardware_ratio=hardware,
        seed=3,
    )
    validation_session = synthetic_session_manifest(
        validation_groups,
        receiver_id="receiver-a",
        session_id="validation",
        boot_id=calibration_session.boot_id,
        radio_epoch=calibration_session.radio_epoch,
    )
    calibration = estimate_chain_calibration(
        calibration_groups,
        mapping,
        session_manifest=calibration_session,
        known_angle_deg=25,
        validation_groups=validation_groups,
        validation_session_manifest=validation_session,
        validation_angle_deg=-25,
        minimum_packets=30,
        synthetic=True,
    )
    target_groups = simulate_strict_groups(
        angle_deg=angle_deg,
        packet_count=packets,
        mapping=mapping,
        hardware_ratio=hardware,
        seed=2,
    )
    target_session = synthetic_session_manifest(
        target_groups,
        receiver_id="receiver-a",
        session_id="target",
        boot_id=calibration_session.boot_id,
        radio_epoch=calibration_session.radio_epoch,
    )
    return mapping, calibration, target_groups, target_session


def test_circular_ratio_calibration_and_17_sector_support() -> None:
    _, calibration, groups, session = _calibration_and_target(angle_deg=31.0)
    result = estimate_coarse_aoa(
        groups,
        calibration,
        capture_manifest=session,
        sector_count=17,
    )
    assert result.peak_angle_deg == pytest.approx(31.0, abs=2.0)
    assert len(result.sectors) == 17
    assert sum(item.normalized_support for item in result.sectors) == pytest.approx(1)
    assert result.normalized_support.sum() == pytest.approx(1)
    assert "front_back_ambiguous" in result.evidence.flags
    assert "limited_packet_sample" not in result.evidence.flags
    assert result.evidence.details["valid_tone_count"] == 64
    assert result.provenance.calibration_id == calibration.calibration_id
    assert result.provenance.start_host_timestamp_ns >= session.start_host_timestamp_ns
    assert result.provenance.end_host_timestamp_ns <= session.end_host_timestamp_ns


def test_13_is_minimum_sector_count_and_sample_gate_is_enforced() -> None:
    _, calibration, groups, session = _calibration_and_target(packets=12)
    result = estimate_coarse_aoa(
        groups,
        calibration,
        capture_manifest=session,
        sector_count=13,
    )
    assert len(result.sectors) == 13
    assert "limited_packet_sample" in result.evidence.flags
    with pytest.raises(ValueError, match="at least 13 sectors"):
        estimate_coarse_aoa(
            groups,
            calibration,
            capture_manifest=session,
            sector_count=12,
        )
    with pytest.raises(ValueError, match="at least 20 paired packets"):
        estimate_coarse_aoa(
            groups,
            calibration,
            capture_manifest=session,
            minimum_packets=20,
        )


def test_calibration_rejects_cross_receiver_boot_epoch_and_source() -> None:
    _, calibration, groups, session = _calibration_and_target()
    for field_name, value in (
        ("receiver_id", "receiver-b"),
        ("boot_id", "different-boot"),
        ("radio_epoch", "different-radio-epoch"),
        ("driver_commit", "different-driver"),
        ("source_tree_hash", "1" * 64),
    ):
        incompatible = replace(session, **{field_name: value})
        with pytest.raises(ValueError, match=field_name):
            estimate_coarse_aoa(
                groups,
                calibration,
                capture_manifest=incompatible,
            )


def test_calibration_id_is_content_hash_and_tamper_is_rejected() -> None:
    _, calibration, _, _ = _calibration_and_target()
    assert calibration.calibration_id.startswith("sha256:")
    data = calibration.to_dict()
    data["phase_concentration"][0] *= 0.9
    with pytest.raises(ValueError, match="artifact hash"):
        ChainCalibration.from_dict(data)
    nonfinite = calibration.to_dict()
    nonfinite["phase_concentration"][0] = float("nan")
    with pytest.raises(ValueError, match="phase_concentration"):
        ChainCalibration.from_dict(nonfinite)
    wrong_type = calibration.to_dict()
    wrong_type["synthetic"] = "false"
    with pytest.raises(ValueError, match="JSON boolean"):
        ChainCalibration.from_dict(wrong_type)


def test_calibration_requires_independent_capture_artifacts() -> None:
    mapping, _, groups, session = _calibration_and_target()
    with pytest.raises(ValueError, match="independent captures"):
        estimate_chain_calibration(
            groups,
            mapping,
            session_manifest=session,
            known_angle_deg=25,
            validation_groups=groups,
            validation_session_manifest=session,
            validation_angle_deg=-25,
            synthetic=True,
        )


def test_distinct_manifest_labels_cannot_hide_identical_holdout_records() -> None:
    mapping = default_mapping(receiver_id="receiver-a")
    groups = simulate_strict_groups(
        angle_deg=25,
        packet_count=36,
        mapping=mapping,
        hardware_ratio=synthetic_hardware_ratio(64),
        seed=41,
    )
    primary = synthetic_session_manifest(
        groups, receiver_id="receiver-a", session_id="primary-identical"
    )
    relabeled_records = [
        replace(
            record,
            sequence=record.sequence + 10_000,
            host_timestamp_ns=record.host_timestamp_ns + 1_000_000_000,
            driver_timestamp=record.driver_timestamp + 20_000,
            packet_sequence_number=record.packet_sequence_number + 100,
            h_idx=record.h_idx + 100,
        )
        for group in groups
        for record in group.records_by_stream.values()
    ]
    relabeled_groups = group_same_ppdu(relabeled_records)
    relabeled = synthetic_session_manifest(
        relabeled_groups,
        receiver_id="receiver-a",
        session_id="validation-relabeled",
        boot_id=primary.boot_id,
        radio_epoch=primary.radio_epoch,
    )
    with pytest.raises(ValueError, match="reuse identical CSI records"):
        estimate_chain_calibration(
            groups,
            mapping,
            session_manifest=primary,
            known_angle_deg=25,
            validation_groups=relabeled_groups,
            validation_session_manifest=relabeled,
            validation_angle_deg=-25,
            synthetic=True,
        )


def test_one_millimeter_baseline_cannot_claim_angle_sign_validation() -> None:
    with pytest.raises(ValueError, match="at least 0.020 m"):
        default_mapping(spacing_m=0.001, receiver_id="receiver-a")
    assert default_mapping(
        spacing_m=0.020,
        receiver_id="receiver-a",
    ).spacing_m == pytest.approx(0.020)


def test_in_memory_calibration_mutation_is_rechecked_before_aoa() -> None:
    _, calibration, groups, session = _calibration_and_target()
    calibration.complex_ratio[0] *= np.exp(0.2j)
    with pytest.raises(ValueError, match="no longer matches its artifact hash"):
        estimate_coarse_aoa(groups, calibration, capture_manifest=session)


def test_mapping_selectors_are_strict_json_integers() -> None:
    mapping = default_mapping(receiver_id="receiver-a")
    data = mapping.to_dict()
    data["tx_idx"] = True
    with pytest.raises(ValueError, match="JSON integer"):
        type(mapping).from_dict(data)


def test_even_fft_bins_and_directed_baseline_prevent_silent_sign_loss() -> None:
    frequencies = subcarrier_frequencies_hz(64, 5500)
    offsets = (frequencies - 5.5e9) / 312_500.0
    assert offsets[0] == -32
    assert offsets[-1] == 31
    mapping = default_mapping(receiver_id="receiver-a")
    reversed_mapping = replace(
        mapping,
        elements=(
            AntennaElement(0, "reference", (0.0, mapping.spacing_m / 2)),
            AntennaElement(1, "target", (0.0, -mapping.spacing_m / 2)),
        ),
    )
    forward = expected_pair_ratio(30.0, mapping, frequencies)
    reversed_ratio = expected_pair_ratio(30.0, reversed_mapping, frequencies)
    assert np.allclose(reversed_ratio, np.conjugate(forward))
    assert not np.allclose(reversed_ratio, forward)


def test_broadside_calibration_is_rejected_because_it_cannot_verify_sign() -> None:
    mapping = default_mapping(receiver_id="receiver-a")
    hardware = synthetic_hardware_ratio(64)
    groups = simulate_strict_groups(
        angle_deg=0,
        packet_count=36,
        mapping=mapping,
        hardware_ratio=hardware,
    )
    session = synthetic_session_manifest(
        groups, receiver_id="receiver-a", session_id="broadside"
    )
    with pytest.raises(ValueError, match="at least 10 degrees"):
        estimate_chain_calibration(
            groups,
            mapping,
            session_manifest=session,
            known_angle_deg=0,
            validation_groups=groups,
            validation_session_manifest=session,
            validation_angle_deg=-25,
        )


def test_opposite_side_holdout_rejects_reversed_chain_geometry() -> None:
    true_mapping = default_mapping(receiver_id="receiver-a")
    reversed_mapping = replace(
        true_mapping,
        elements=(
            AntennaElement(0, "reference", (0.0, true_mapping.spacing_m / 2)),
            AntennaElement(1, "target", (0.0, -true_mapping.spacing_m / 2)),
        ),
    )
    hardware = synthetic_hardware_ratio(64)
    left_groups = simulate_strict_groups(
        angle_deg=25,
        packet_count=36,
        mapping=true_mapping,
        hardware_ratio=hardware,
        seed=31,
    )
    right_groups = simulate_strict_groups(
        angle_deg=-25,
        packet_count=36,
        mapping=true_mapping,
        hardware_ratio=hardware,
        seed=32,
    )
    left_session = synthetic_session_manifest(
        left_groups, receiver_id="receiver-a", session_id="left"
    )
    right_session = synthetic_session_manifest(
        right_groups,
        receiver_id="receiver-a",
        session_id="right",
        boot_id=left_session.boot_id,
        radio_epoch=left_session.radio_epoch,
    )
    with pytest.raises(ValueError, match="opposite-side calibration disagrees"):
        estimate_chain_calibration(
            left_groups,
            reversed_mapping,
            session_manifest=left_session,
            known_angle_deg=25,
            validation_groups=right_groups,
            validation_session_manifest=right_session,
            validation_angle_deg=-25,
            synthetic=True,
        )


def test_grating_lobes_and_front_back_candidates_are_enumerated() -> None:
    wavelength = 299_792_458.0 / 5.5e9
    candidates = enumerate_grating_lobe_candidates(
        wrapped_phase_rad=0.4,
        baseline_vector_m=(0.0, 1.3 * wavelength),
        center_frequency_hz=5.5e9,
        broadside_heading_deg=20,
    )
    assert len(candidates) >= 2
    assert all(0 <= candidate.front_bearing_deg < 360 for candidate in candidates)
    assert all(0 <= candidate.back_bearing_deg < 360 for candidate in candidates)
    assert any(
        candidate.front_bearing_deg != candidate.back_bearing_deg
        for candidate in candidates
    )


def test_alias_candidates_preserve_angle_when_pair_direction_is_reversed() -> None:
    frequency_hz = 5.5e9
    spacing_m = 0.028
    angle_deg = 30.0
    arrival = np.asarray([np.cos(np.deg2rad(angle_deg)), np.sin(np.deg2rad(angle_deg))])
    forward_baseline = np.asarray([0.0, spacing_m])
    reverse_baseline = -forward_baseline

    def wrapped_phase(baseline: np.ndarray) -> float:
        raw = (
            -2.0
            * np.pi
            * float(np.dot(baseline, arrival))
            * frequency_hz
            / 299_792_458.0
        )
        return float(np.angle(np.exp(1j * raw)))

    forward = enumerate_grating_lobe_candidates(
        wrapped_phase(forward_baseline), forward_baseline, frequency_hz
    )
    reverse = enumerate_grating_lobe_candidates(
        wrapped_phase(reverse_baseline), reverse_baseline, frequency_hz
    )
    assert [item.angle_deg for item in forward] == pytest.approx(
        [item.angle_deg for item in reverse]
    )
    assert any(item.angle_deg == pytest.approx(angle_deg) for item in forward)
