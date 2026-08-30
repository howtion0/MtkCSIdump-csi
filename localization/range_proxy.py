"""Environment-calibrated near/mid/far classification, never absolute ToF."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .contracts import RadioToneConfig, validate_analysis_record
from .grouping import GroupedPPDU
from .jsonio import dump_json, json_safe, load_json
from .models import Evidence, normalize_mac
from .session import SessionManifest

DEFAULT_FEATURE_NAMES = (
    "median_rssi_raw",
    "median_log_csi_power",
    "amplitude_cv",
    "median_snr_raw",
)


def _validate_artifact_id(value: str, name: str) -> None:
    digest = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{name} must be a SHA-256 artifact ID")


def _strict_json_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _strict_json_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a JSON integer")
    return value


def _strict_json_str(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a JSON string")
    return value


def _strict_numeric_array(value: object, name: str, dimensions: int) -> np.ndarray:
    try:
        raw = np.asarray(value, dtype=object)
    except ValueError as exc:
        raise ValueError(f"{name} must be a rectangular JSON numeric array") from exc
    if raw.ndim != dimensions or raw.size == 0:
        raise ValueError(f"{name} has the wrong array dimensions")
    if any(
        type(item) not in {int, float} or not np.isfinite(item) for item in raw.flat
    ):
        raise ValueError(f"{name} must contain only finite JSON numbers")
    return raw.astype(float)


@dataclass(frozen=True)
class RangeFeatureProvenance:
    capture_manifest_id: str
    receiver_id: str
    transmitter_address: str
    start_host_timestamp_ns: int
    end_host_timestamp_ns: int
    radio_config: RadioToneConfig

    def __post_init__(self) -> None:
        _validate_artifact_id(self.capture_manifest_id, "capture manifest ID")
        if type(self.receiver_id) is not str or not self.receiver_id.strip():
            raise ValueError("range feature receiver_id is required")
        object.__setattr__(
            self, "transmitter_address", normalize_mac(self.transmitter_address)
        )
        if (
            type(self.start_host_timestamp_ns) is not int
            or type(self.end_host_timestamp_ns) is not int
            or self.start_host_timestamp_ns < 0
            or self.end_host_timestamp_ns < self.start_host_timestamp_ns
        ):
            raise ValueError("range feature time window is invalid")
        if not isinstance(self.radio_config, RadioToneConfig):
            raise TypeError("range feature radio_config has the wrong type")

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_manifest_id": self.capture_manifest_id,
            "receiver_id": self.receiver_id,
            "transmitter_address": self.transmitter_address,
            "actual_used_window": {
                "start_host_timestamp_ns": self.start_host_timestamp_ns,
                "end_host_timestamp_ns": self.end_host_timestamp_ns,
            },
            "radio_config": self.radio_config.to_dict(),
        }


@dataclass(frozen=True)
class BoundRangeFeatures:
    values: dict[str, float]
    provenance: RangeFeatureProvenance


@dataclass
class RangeEstimate:
    label: str
    support_weights: dict[str, float]
    evidence: Evidence
    nearest_standardized_distance: float
    model_id: str
    device_id: str
    room_id: str
    class_distance_bands_m: dict[str, tuple[float, float]]
    feature_provenance: RangeFeatureProvenance | None = None

    def __post_init__(self) -> None:
        _validate_artifact_id(self.model_id, "range model_id")
        if (
            type(self.device_id) is not str
            or type(self.room_id) is not str
            or not self.device_id.strip()
            or not self.room_id.strip()
        ):
            raise ValueError("range device_id and room_id are required")
        if not isinstance(self.evidence, Evidence):
            raise TypeError("range evidence has the wrong type")
        if self.feature_provenance is not None and not isinstance(
            self.feature_provenance, RangeFeatureProvenance
        ):
            raise TypeError("range feature provenance has the wrong type")
        if not np.isfinite(self.nearest_standardized_distance) or (
            self.nearest_standardized_distance < 0
        ):
            raise ValueError(
                "nearest standardized distance must be finite/non-negative"
            )
        if (
            not self.label.strip()
            or self.label not in self.support_weights
            or set(self.support_weights) != set(self.class_distance_bands_m)
        ):
            raise ValueError("range label/support/band definitions disagree")
        total = float(sum(self.support_weights.values()))
        if (
            not np.isfinite(total)
            or total <= 0
            or any(
                not np.isfinite(value) or value < 0
                for value in self.support_weights.values()
            )
        ):
            raise ValueError("range support weights are invalid")
        self.support_weights = {
            label: float(value / total) for label, value in self.support_weights.items()
        }
        for label, (lower, upper) in self.class_distance_bands_m.items():
            if (
                not label.strip()
                or not np.isfinite(lower)
                or lower < 0
                or np.isnan(upper)
                or upper <= lower
            ):
                raise ValueError("range distance bands are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "quantity": "labeled_range_proxy",
            "label": self.label,
            "support_weights": self.support_weights,
            "nearest_standardized_distance": self.nearest_standardized_distance,
            "model_id": self.model_id,
            "device_id": self.device_id,
            "room_id": self.room_id,
            "class_distance_bands_m": {
                label: [float(lower), float(upper) if np.isfinite(upper) else None]
                for label, (lower, upper) in self.class_distance_bands_m.items()
            },
            "feature_provenance": (
                None
                if self.feature_provenance is None
                else self.feature_provenance.to_dict()
            ),
            "evidence": {
                "score": self.evidence.score,
                "flags": list(self.evidence.flags),
                "details": self.evidence.details,
            },
        }


def extract_range_features(
    groups: Iterable[GroupedPPDU],
    *,
    minimum_packets: int = 12,
    minimum_valid_tone_fraction: float = 0.75,
) -> dict[str, float]:
    """Extract hardware-relative features from a short packet window."""

    if type(minimum_packets) is not int or minimum_packets < 2:
        raise ValueError("minimum_packets must be an integer of at least two")
    if (
        type(minimum_valid_tone_fraction) not in {int, float}
        or not np.isfinite(minimum_valid_tone_fraction)
        or not 0 < minimum_valid_tone_fraction <= 1
    ):
        raise ValueError("minimum_valid_tone_fraction must be in (0, 1]")
    materialized = list(groups)
    if len(materialized) < minimum_packets:
        raise ValueError(
            f"range window needs at least {minimum_packets} packets; got {len(materialized)}"
        )
    if any(group.hard_failure_flags for group in materialized):
        raise ValueError("range window contains a hard-failed PPDU")
    records = [
        record for group in materialized for record in group.records_by_stream.values()
    ]
    if not records:
        raise ValueError("range features require at least one CSI record")
    configs = {validate_analysis_record(record).signature() for record in records}
    if len(configs) != 1:
        raise ValueError("range window must use one radio/tone config")
    if len({record.transmitter_address for record in records}) != 1:
        raise ValueError("range window must contain one transmitter address")
    valid_tone_fractions = [
        float(np.count_nonzero(np.abs(record.samples) > 1e-9) / record.sample_count)
        for record in records
    ]
    if min(valid_tone_fractions) < minimum_valid_tone_fraction:
        raise ValueError("range window has too few valid tones")
    rssis = np.asarray(
        [record.rssi_raw for record in records if record.rssi_raw is not None],
        dtype=float,
    )
    snrs = np.asarray(
        [record.snr_raw for record in records if record.snr_raw is not None],
        dtype=float,
    )
    if rssis.size != len(records) or snrs.size != len(records):
        raise ValueError("range features require RSSI/SNR telemetry on every record")
    amplitudes = np.concatenate([np.abs(record.samples) for record in records])
    power_per_record = np.asarray(
        [np.mean(np.abs(record.samples) ** 2) for record in records], dtype=float
    )
    mean_amplitude = float(np.mean(amplitudes))
    return {
        "median_rssi_raw": float(np.median(rssis)),
        "median_log_csi_power": float(np.median(np.log10(power_per_record + 1e-12))),
        "amplitude_cv": float(np.std(amplitudes) / max(mean_amplitude, 1e-12)),
        "median_snr_raw": float(np.median(snrs)),
    }


def extract_bound_range_features(
    groups: Iterable[GroupedPPDU],
    *,
    session_manifest: SessionManifest,
    capture_path: str | Path | None = None,
    minimum_packets: int = 12,
    minimum_valid_tone_fraction: float = 0.75,
) -> BoundRangeFeatures:
    """Extract features and bind the exact records to one verified manifest."""

    materialized = list(groups)
    records = [
        record for group in materialized for record in group.records_by_stream.values()
    ]
    values = extract_range_features(
        materialized,
        minimum_packets=minimum_packets,
        minimum_valid_tone_fraction=minimum_valid_tone_fraction,
    )
    session_manifest.assert_records_verified(records, capture_path=capture_path)
    transmitters = {record.transmitter_address for record in records}
    if len(transmitters) != 1:
        raise ValueError("bound range features require one transmitter")
    return BoundRangeFeatures(
        values=values,
        provenance=RangeFeatureProvenance(
            capture_manifest_id=session_manifest.computed_artifact_id(),
            receiver_id=session_manifest.receiver_id,
            transmitter_address=next(iter(transmitters)),
            start_host_timestamp_ns=min(record.host_timestamp_ns for record in records),
            end_host_timestamp_ns=max(record.host_timestamp_ns for record in records),
            radio_config=session_manifest.radio_config,
        ),
    )


def feature_vector(
    features: dict[str, float], feature_names: Sequence[str] = DEFAULT_FEATURE_NAMES
) -> np.ndarray:
    try:
        vector = np.asarray([features[name] for name in feature_names], dtype=float)
    except KeyError as exc:
        raise ValueError(f"missing range feature {exc.args[0]}") from exc
    if not np.all(np.isfinite(vector)):
        raise ValueError("range feature vector contains non-finite values")
    return vector


@dataclass
class KNNRangeProxy:
    """Small labeled classifier with explicit room/device provenance."""

    feature_names: tuple[str, ...]
    training_features: np.ndarray
    training_labels: np.ndarray
    center: np.ndarray
    scale: np.ndarray
    k: int
    room_id: str
    training_device_ids: tuple[str, ...]
    training_source_ids: tuple[str, ...]
    class_distance_bands_m: dict[str, tuple[float, float]]
    synthetic: bool = False
    notes: str = ""
    schema_version: int = 2

    def __post_init__(self) -> None:
        self.training_features = np.asarray(self.training_features, dtype=float)
        self.training_labels = np.asarray(self.training_labels, dtype=str)
        self.center = np.asarray(self.center, dtype=float)
        self.scale = np.asarray(self.scale, dtype=float)
        if self.training_features.ndim != 2 or self.training_features.shape[0] == 0:
            raise ValueError("training_features must be a non-empty matrix")
        if self.training_features.shape[1] != len(self.feature_names):
            raise ValueError("feature_names do not match training matrix")
        if self.training_labels.shape != (self.training_features.shape[0],):
            raise ValueError("training labels do not match training rows")
        if (
            self.center.shape != (self.training_features.shape[1],)
            or self.scale.shape != self.center.shape
        ):
            raise ValueError("normalization vectors have the wrong shape")
        if (
            np.any(self.scale <= 0)
            or not np.all(np.isfinite(self.scale))
            or not np.all(np.isfinite(self.center))
            or not np.all(np.isfinite(self.training_features))
        ):
            raise ValueError("training data and normalization must be finite")
        if (
            type(self.k) is not int
            or not 1 <= self.k <= self.training_features.shape[0]
        ):
            raise ValueError("k is outside the training set")
        if type(self.synthetic) is not bool:
            raise ValueError("synthetic must be boolean")
        if type(self.notes) is not str:
            raise ValueError("notes must be a string")
        if type(self.room_id) is not str or type(self.training_device_ids) is not tuple:
            raise ValueError("training room/device provenance has the wrong type")
        if type(self.training_source_ids) is not tuple:
            raise ValueError("training source provenance has the wrong type")
        if (
            not self.room_id.strip()
            or not self.training_device_ids
            or not self.training_source_ids
        ):
            raise ValueError("training room and at least one device ID are required")
        if any(
            type(value) is not str or not value.strip()
            for value in self.training_device_ids
        ):
            raise ValueError("training device IDs cannot be empty")
        for source_id in self.training_source_ids:
            _validate_artifact_id(source_id, "training source ID")
        if len(set(self.training_source_ids)) != len(self.training_source_ids):
            raise ValueError("training source IDs must be unique")
        if any(not label.strip() for label in self.training_labels):
            raise ValueError("training labels cannot be empty")
        if len(set(self.feature_names)) != len(self.feature_names) or any(
            not name.strip() for name in self.feature_names
        ):
            raise ValueError("feature names must be non-empty and unique")
        if set(self.class_distance_bands_m) != set(self.training_labels):
            raise ValueError("range bands must exactly match trained classes")
        for label, pair in self.class_distance_bands_m.items():
            lower, upper = pair
            if (
                not label
                or not np.isfinite(lower)
                or lower < 0
                or np.isnan(upper)
                or upper <= lower
            ):
                raise ValueError("range label bands are invalid")

    @classmethod
    def fit(
        cls,
        training_features: np.ndarray,
        training_labels: Sequence[str],
        *,
        room_id: str,
        training_device_ids: Sequence[str],
        training_source_ids: Sequence[str],
        feature_names: Sequence[str] = DEFAULT_FEATURE_NAMES,
        k: int = 5,
        class_distance_bands_m: dict[str, tuple[float, float]] | None = None,
        synthetic: bool = False,
        notes: str = "",
    ) -> KNNRangeProxy:
        if type(k) is not int or k < 1:
            raise ValueError("k must be a positive integer")
        if type(room_id) is not str or not room_id.strip():
            raise ValueError("room_id is required")
        if any(type(value) is not str for value in training_device_ids):
            raise ValueError("training device IDs must be strings")
        if any(type(value) is not str for value in training_source_ids):
            raise ValueError("training source IDs must be strings")
        features = np.asarray(training_features, dtype=float)
        labels = np.asarray(training_labels, dtype=str)
        if features.ndim != 2 or features.shape[0] < 3:
            raise ValueError("at least three labeled training rows are required")
        if labels.shape != (features.shape[0],):
            raise ValueError("labels do not match training rows")
        if not np.all(np.isfinite(features)):
            raise ValueError("training features contain non-finite values")
        center = np.median(features, axis=0)
        mad = np.median(np.abs(features - center), axis=0) * 1.4826
        standard_deviation = np.std(features, axis=0)
        scale = np.where(
            mad > 1e-9,
            mad,
            np.where(standard_deviation > 1e-9, standard_deviation, 1.0),
        )
        bands = (
            {
                "near": (0.0, 1.5),
                "mid": (1.5, 3.5),
                "far": (3.5, float("inf")),
            }
            if class_distance_bands_m is None
            else class_distance_bands_m
        )
        missing = set(np.unique(labels)) - set(bands)
        if missing:
            raise ValueError(
                f"distance-band metadata missing for labels: {sorted(missing)}"
            )
        return cls(
            feature_names=tuple(feature_names),
            training_features=features,
            training_labels=labels,
            center=center,
            scale=scale,
            k=min(k, features.shape[0]),
            room_id=room_id,
            training_device_ids=tuple(sorted(set(training_device_ids))),
            training_source_ids=tuple(sorted(set(training_source_ids))),
            class_distance_bands_m=bands,
            synthetic=synthetic,
            notes=notes,
        )

    @property
    def classes(self) -> tuple[str, ...]:
        preferred = [
            name for name in ("near", "mid", "far") if name in self.training_labels
        ]
        remainder = sorted(set(self.training_labels) - set(preferred))
        return (*preferred, *remainder)

    def computed_artifact_id(self) -> str:
        encoded = json.dumps(
            json_safe(self.to_dict()),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def predict(
        self,
        features: np.ndarray | dict[str, float],
        *,
        device_id: str,
        room_id: str,
        feature_provenance: RangeFeatureProvenance | None = None,
    ) -> RangeEstimate:
        if feature_provenance is not None and (
            normalize_mac(device_id) != feature_provenance.transmitter_address
        ):
            raise ValueError("feature provenance TA differs from prediction device_id")
        vector = (
            feature_vector(features, self.feature_names)
            if isinstance(features, dict)
            else np.asarray(features, dtype=float)
        )
        if vector.shape != self.center.shape or not np.all(np.isfinite(vector)):
            raise ValueError(
                "prediction feature vector has the wrong shape or non-finite values"
            )
        normalized_training = (self.training_features - self.center) / self.scale
        normalized = (vector - self.center) / self.scale
        distances = np.linalg.norm(normalized_training - normalized, axis=1)
        nearest_indices = np.argsort(distances, kind="stable")[: self.k]
        nearest_distances = distances[nearest_indices]
        weights = 1.0 / np.maximum(nearest_distances, 0.05)
        class_weights = {name: 0.0 for name in self.classes}
        for index, weight in zip(nearest_indices, weights):
            class_weights[str(self.training_labels[index])] += float(weight)
        total = sum(class_weights.values()) or 1.0
        support_weights = {name: value / total for name, value in class_weights.items()}
        label = max(support_weights, key=support_weights.get)
        sorted_support = sorted(support_weights.values(), reverse=True)
        margin = sorted_support[0] - (
            sorted_support[1] if len(sorted_support) > 1 else 0.0
        )
        nearest = float(nearest_distances[0])
        coverage = float(np.exp(-max(0.0, nearest - 1.0) / 2.0))
        score = float(np.clip((0.35 + 0.65 * margin) * coverage, 0.0, 1.0))
        flags = ["environment_labeled_proxy", "not_tof", "not_absolute_range"]
        flags.append(
            "capture_bound_feature_window"
            if feature_provenance is not None
            else "not_capture_bound"
        )
        if device_id not in self.training_device_ids:
            flags.append("unseen_transmitter_device")
            score *= 0.62
        if room_id != self.room_id:
            flags.append("room_domain_shift")
            score *= 0.45
        if nearest > 3.0:
            flags.append("feature_domain_shift")
            score *= 0.55
        if nearest > 5.0:
            flags.append("out_of_distribution")
            score *= 0.45
        return RangeEstimate(
            label=label,
            support_weights=support_weights,
            nearest_standardized_distance=nearest,
            evidence=Evidence(
                score=float(np.clip(score, 0.0, 1.0)),
                flags=tuple(flags),
                details={
                    "room_id": room_id,
                    "training_room_id": self.room_id,
                    "device_id": device_id,
                    "training_device_ids": list(self.training_device_ids),
                    "training_source_ids": list(self.training_source_ids),
                    "display_band_m": list(self.class_distance_bands_m[label]),
                    "display_band_is_label_definition_not_measured_distance": True,
                },
            ),
            model_id=self.computed_artifact_id(),
            device_id=device_id,
            room_id=room_id,
            class_distance_bands_m=dict(self.class_distance_bands_m),
            feature_provenance=feature_provenance,
        )

    def to_dict(self) -> dict[str, object]:
        def finite_band(pair: tuple[float, float]) -> list[float | None]:
            return [float(pair[0]), float(pair[1]) if np.isfinite(pair[1]) else None]

        return {
            "schema": "ax3000t-knn-range-proxy",
            "schema_version": self.schema_version,
            "feature_names": list(self.feature_names),
            "training_features": self.training_features.tolist(),
            "training_labels": self.training_labels.tolist(),
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "k": self.k,
            "room_id": self.room_id,
            "training_device_ids": list(self.training_device_ids),
            "training_source_ids": list(self.training_source_ids),
            "synthetic": self.synthetic,
            "notes": self.notes,
            "class_distance_bands_m": {
                label: finite_band(pair)
                for label, pair in self.class_distance_bands_m.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> KNNRangeProxy:
        if (
            data.get("schema") != "ax3000t-knn-range-proxy"
            or _strict_json_int(data.get("schema_version"), "schema_version") != 2
        ):
            raise ValueError("unsupported range-proxy schema")
        bands_data = data.get("class_distance_bands_m")
        if type(bands_data) is not dict:
            raise ValueError("class_distance_bands_m must be a JSON object")
        bands: dict[str, tuple[float, float]] = {}
        for raw_label, pair in bands_data.items():
            label = _strict_json_str(raw_label, "range band label")
            if type(pair) is not list or len(pair) != 2:
                raise ValueError("range band must be a two-item JSON array")
            lower_raw, upper_raw = pair
            if type(lower_raw) not in {int, float} or not np.isfinite(lower_raw):
                raise ValueError("range band lower bound must be finite")
            if upper_raw is not None and (
                type(upper_raw) not in {int, float} or not np.isfinite(upper_raw)
            ):
                raise ValueError("range band upper bound must be finite or null")
            bands[label] = (
                float(lower_raw),
                float(upper_raw) if upper_raw is not None else float("inf"),
            )
        labels_data = data.get("training_labels")
        if type(labels_data) is not list:
            raise ValueError("training_labels must be a JSON array")
        return cls(
            feature_names=tuple(
                _strict_json_str(value, "feature_names[]")
                for value in data["feature_names"]
            ),
            training_features=_strict_numeric_array(
                data["training_features"], "training_features", 2
            ),
            training_labels=np.asarray(
                [_strict_json_str(value, "training_labels[]") for value in labels_data],
                dtype=str,
            ),
            center=_strict_numeric_array(data["center"], "center", 1),
            scale=_strict_numeric_array(data["scale"], "scale", 1),
            k=_strict_json_int(data["k"], "k"),
            room_id=_strict_json_str(data["room_id"], "room_id"),
            training_device_ids=tuple(
                _strict_json_str(value, "training_device_ids[]")
                for value in data["training_device_ids"]
            ),
            training_source_ids=tuple(
                _strict_json_str(value, "training_source_ids[]")
                for value in data["training_source_ids"]
            ),
            class_distance_bands_m=bands,
            synthetic=_strict_json_bool(data.get("synthetic", False), "synthetic"),
            notes=_strict_json_str(data.get("notes", ""), "notes"),
            schema_version=_strict_json_int(data["schema_version"], "schema_version"),
        )

    def save(self, path: str | Path) -> None:
        dump_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> KNNRangeProxy:
        return cls.from_dict(load_json(path))
