from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from localization.range_proxy import (
    KNNRangeProxy,
    extract_bound_range_features,
    extract_range_features,
)
from localization.simulate import (
    default_mapping,
    simulate_strict_groups,
    synthetic_range_dataset,
    synthetic_session_manifest,
)


def _model() -> KNNRangeProxy:
    features, labels = synthetic_range_dataset(seed=1, samples_per_class=30)
    return KNNRangeProxy.fit(
        features,
        labels,
        room_id="lab-A",
        training_device_ids=["phone-A"],
        training_source_ids=["sha256:" + "1" * 64],
        k=5,
    )


def test_labeled_knn_predicts_near_mid_far_and_round_trips(tmp_path) -> None:
    model = _model()
    centers = {
        "near": np.asarray([-43.0, 6.20, 0.28, 32.0]),
        "mid": np.asarray([-56.0, 5.25, 0.42, 23.0]),
        "far": np.asarray([-70.0, 4.35, 0.59, 14.0]),
    }
    for label, vector in centers.items():
        estimate = model.predict(vector, device_id="phone-A", room_id="lab-A")
        assert estimate.label == label
        assert sum(estimate.support_weights.values()) == pytest.approx(1.0)
        assert "not_absolute_range" in estimate.evidence.flags
        assert "not_capture_bound" in estimate.evidence.flags
        assert estimate.model_id == model.computed_artifact_id()
        assert estimate.device_id == "phone-A"
        assert estimate.room_id == "lab-A"
        assert estimate.evidence.details["training_source_ids"] == [
            "sha256:" + "1" * 64
        ]
    path = tmp_path / "range.json"
    model.save(path)
    loaded = KNNRangeProxy.load(path)
    assert (
        loaded.predict(centers["mid"], device_id="phone-A", room_id="lab-A").label
        == "mid"
    )
    assert loaded.computed_artifact_id() == model.computed_artifact_id()


def test_range_model_requires_strict_training_provenance() -> None:
    model = _model()
    malformed = model.to_dict()
    malformed["synthetic"] = "false"
    with pytest.raises(ValueError, match="JSON boolean"):
        KNNRangeProxy.from_dict(malformed)
    missing = model.to_dict()
    missing["training_source_ids"] = []
    with pytest.raises(ValueError, match="training room"):
        KNNRangeProxy.from_dict(missing)
    invalid = model.to_dict()
    invalid["training_source_ids"] = ["unbound-training-data"]
    with pytest.raises(ValueError, match="SHA-256"):
        KNNRangeProxy.from_dict(invalid)


def test_device_change_causes_deterministic_generalization_degradation() -> None:
    model = _model()
    same_features, labels = synthetic_range_dataset(seed=2, samples_per_class=15)
    changed_features, changed_labels = synthetic_range_dataset(
        seed=2,
        samples_per_class=15,
        device_shift=np.asarray([-13.0, -0.95, 0.14, -9.0]),
    )
    same_predictions = [
        model.predict(row, device_id="phone-A", room_id="lab-A")
        for row in same_features
    ]
    changed_predictions = [
        model.predict(row, device_id="phone-B", room_id="lab-A")
        for row in changed_features
    ]
    same_accuracy = np.mean(
        [
            prediction.label == label
            for prediction, label in zip(same_predictions, labels)
        ]
    )
    changed_accuracy = np.mean(
        [
            prediction.label == label
            for prediction, label in zip(changed_predictions, changed_labels)
        ]
    )
    assert same_accuracy >= 0.95
    assert changed_accuracy <= same_accuracy - 0.30
    assert all(
        "unseen_transmitter_device" in prediction.evidence.flags
        for prediction in changed_predictions
    )
    assert np.mean([item.evidence.score for item in changed_predictions]) < np.mean(
        [item.evidence.score for item in same_predictions]
    )


def test_large_feature_shift_is_flagged_out_of_distribution() -> None:
    estimate = _model().predict(
        np.asarray([10.0, 12.0, 2.0, 70.0]),
        device_id="phone-B",
        room_id="lab-B",
    )
    assert "feature_domain_shift" in estimate.evidence.flags
    assert "out_of_distribution" in estimate.evidence.flags
    assert "room_domain_shift" in estimate.evidence.flags


def test_knn_rejects_nonfinite_model_state() -> None:
    model = _model()
    with pytest.raises(ValueError, match="finite"):
        replace(model, center=np.asarray([np.nan, 0.0, 0.0, 0.0]))
    bad_bands = dict(model.class_distance_bands_m)
    bad_bands["far"] = (3.5, float("nan"))
    with pytest.raises(ValueError, match="bands"):
        replace(model, class_distance_bands_m=bad_bands)


def test_feature_extraction_requires_one_ta_radio_config_and_packet_floor() -> None:
    groups = simulate_strict_groups(
        angle_deg=0,
        packet_count=12,
        mapping=default_mapping(receiver_id="receiver-a"),
    )
    features = extract_range_features(groups)
    assert set(features) == {
        "median_rssi_raw",
        "median_log_csi_power",
        "amplitude_cv",
        "median_snr_raw",
    }
    manifest = synthetic_session_manifest(
        groups, receiver_id="receiver-a", session_id="range-bound"
    )
    bound = extract_bound_range_features(groups, session_manifest=manifest)
    assert bound.provenance.capture_manifest_id == manifest.computed_artifact_id()
    assert bound.provenance.receiver_id == "receiver-a"
    assert bound.provenance.transmitter_address == "02:00:00:00:00:01"
    assert bound.values == features
    unverified_real_manifest = replace(
        manifest,
        synthetic=False,
        driver_commit="1dec35f376944e62bf826256cd1882b9c89080ce",
    )
    with pytest.raises(ValueError, match="bytes were not verified"):
        extract_bound_range_features(
            groups,
            session_manifest=unverified_real_manifest,
        )
    with pytest.raises(ValueError, match="differs from prediction device"):
        _model().predict(
            bound.values,
            device_id="02:00:00:00:00:02",
            room_id="lab-A",
            feature_provenance=bound.provenance,
        )
    with pytest.raises(ValueError, match="at least 13 packets"):
        extract_range_features(groups, minimum_packets=13)
    missing_telemetry_groups = simulate_strict_groups(
        angle_deg=0,
        packet_count=12,
        mapping=default_mapping(receiver_id="receiver-a"),
    )
    first_group = missing_telemetry_groups[0]
    first_key = next(iter(first_group.records_by_stream))
    first_group.records_by_stream[first_key] = replace(
        first_group.records_by_stream[first_key], rssi_raw=None
    )
    with pytest.raises(ValueError, match="RSSI/SNR"):
        extract_range_features(missing_telemetry_groups)
    zeroed = []
    for group in groups:
        group.records_by_stream = {
            key: replace(record, samples=np.zeros(64, dtype=complex))
            for key, record in group.records_by_stream.items()
        }
        zeroed.append(group)
    with pytest.raises(ValueError, match="too few valid tones"):
        extract_range_features(zeroed)
