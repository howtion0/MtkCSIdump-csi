"""Normalized 2-D display-support fusion across independent receivers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .aoa import AoAEstimate
from .contracts import RadioToneConfig
from .models import Evidence, normalize_mac, normalized_entropy
from .range_proxy import RangeEstimate, RangeFeatureProvenance


def _wrap_degrees(angle: np.ndarray) -> np.ndarray:
    return (angle + 180.0) % 360.0 - 180.0


@dataclass
class APObservation:
    receiver_id: str
    calibration_id: str
    capture_manifest_id: str
    transmitter_address: str
    start_host_timestamp_ns: int
    end_host_timestamp_ns: int
    timebase_id: str
    clock_uncertainty_ns: int
    radio_config: RadioToneConfig
    position_m: tuple[float, float]
    broadside_heading_deg: float
    angle_grid_deg: np.ndarray
    angle_normalized_support: np.ndarray
    evidence_score: float
    front_back_ambiguous: bool = True
    range_support_weights: dict[str, float] | None = None
    range_bands_m: dict[str, tuple[float, float]] | None = None
    range_evidence_score: float | None = None
    range_evidence_flags: tuple[str, ...] = ()
    range_model_id: str | None = None
    range_device_id: str | None = None
    range_room_id: str | None = None
    range_feature_provenance: RangeFeatureProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.radio_config, RadioToneConfig):
            raise TypeError("observation radio_config has the wrong type")
        if type(self.front_back_ambiguous) is not bool:
            raise ValueError("front_back_ambiguous must be boolean")
        self.transmitter_address = normalize_mac(self.transmitter_address)
        self.angle_grid_deg = np.asarray(self.angle_grid_deg, dtype=float)
        self.angle_normalized_support = np.asarray(
            self.angle_normalized_support, dtype=float
        )
        if (
            len(self.position_m) != 2
            or not np.all(np.isfinite(self.position_m))
            or not np.isfinite(self.broadside_heading_deg)
        ):
            raise ValueError("receiver position and heading must be finite")
        if not np.all(np.isfinite(self.angle_grid_deg)):
            raise ValueError("angle grid must be finite")
        if not self.receiver_id.strip() or not self.calibration_id.strip():
            raise ValueError("receiver_id and calibration_id are required")
        for field_name, artifact_id in (
            ("calibration_id", self.calibration_id),
            ("capture_manifest_id", self.capture_manifest_id),
        ):
            digest = artifact_id.removeprefix("sha256:")
            if (
                not artifact_id.startswith("sha256:")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be a SHA-256 artifact ID")
        if (
            type(self.start_host_timestamp_ns) is not int
            or type(self.end_host_timestamp_ns) is not int
            or type(self.clock_uncertainty_ns) is not int
        ):
            raise ValueError("observation time values must be integers")
        if self.end_host_timestamp_ns < self.start_host_timestamp_ns:
            raise ValueError("observation time window is inverted")
        if not self.timebase_id.strip() or self.clock_uncertainty_ns < 0:
            raise ValueError("timebase ID and non-negative uncertainty are required")
        if self.angle_grid_deg.ndim != 1 or self.angle_grid_deg.size < 2:
            raise ValueError("angle grid must contain at least two points")
        if self.angle_normalized_support.shape != self.angle_grid_deg.shape:
            raise ValueError("angle support does not match the grid")
        if np.any(np.diff(self.angle_grid_deg) <= 0):
            raise ValueError("angle grid must be strictly increasing")
        if (
            np.any(~np.isfinite(self.angle_normalized_support))
            or np.any(self.angle_normalized_support < 0)
            or self.angle_normalized_support.sum() <= 0
        ):
            raise ValueError("angle support must be finite, non-negative, and non-zero")
        self.angle_normalized_support /= self.angle_normalized_support.sum()
        if not np.isfinite(self.evidence_score) or not 0 < self.evidence_score <= 1:
            raise ValueError("zero/invalid evidence observations are rejected")
        range_material = (
            self.range_support_weights,
            self.range_bands_m,
            self.range_evidence_score,
            self.range_model_id,
            self.range_device_id,
            self.range_room_id,
            self.range_feature_provenance,
        )
        if any(value is None for value in range_material) and any(
            value is not None for value in range_material
        ):
            raise ValueError(
                "complete range estimate provenance must be provided together"
            )
        if self.range_support_weights is not None:
            if set(self.range_support_weights) != set(self.range_bands_m):
                raise ValueError("range support labels do not match range bands")
            total = sum(self.range_support_weights.values())
            if (
                not np.isfinite(total)
                or total <= 0
                or any(
                    not np.isfinite(value)
                    for value in self.range_support_weights.values()
                )
                or any(value < 0 for value in self.range_support_weights.values())
            ):
                raise ValueError("range support weights are invalid")
            for lower, upper in self.range_bands_m.values():
                if (
                    not np.isfinite(lower)
                    or lower < 0
                    or np.isnan(upper)
                    or upper <= lower
                ):
                    raise ValueError("range bands are invalid")
            self.range_support_weights = {
                label: float(value / total)
                for label, value in self.range_support_weights.items()
            }
            if (
                not np.isfinite(self.range_evidence_score)
                or not 0 <= self.range_evidence_score <= 1
            ):
                raise ValueError("range evidence score must be finite and in [0, 1]")
            model_digest = self.range_model_id.removeprefix("sha256:")
            if (
                not self.range_model_id.startswith("sha256:")
                or len(model_digest) != 64
                or any(
                    character not in "0123456789abcdef" for character in model_digest
                )
            ):
                raise ValueError("range model_id must be a SHA-256 artifact ID")
            if self.range_device_id != self.transmitter_address:
                raise ValueError("range device_id is not bound to the AoA transmitter")
            if not self.range_room_id.strip():
                raise ValueError("range room_id is required")
            feature_provenance = self.range_feature_provenance
            if feature_provenance.capture_manifest_id != self.capture_manifest_id:
                raise ValueError(
                    "range features and AoA use different capture manifests"
                )
            if feature_provenance.receiver_id != self.receiver_id:
                raise ValueError("range features and AoA use different receivers")
            if feature_provenance.transmitter_address != self.transmitter_address:
                raise ValueError("range feature TA differs from the AoA transmitter")
            if (
                feature_provenance.radio_config.signature()
                != self.radio_config.signature()
            ):
                raise ValueError("range features and AoA use different radio profiles")
            overlap_start = max(
                feature_provenance.start_host_timestamp_ns,
                self.start_host_timestamp_ns,
            )
            overlap_end = min(
                feature_provenance.end_host_timestamp_ns,
                self.end_host_timestamp_ns,
            )
            if overlap_end < overlap_start:
                raise ValueError("range feature and AoA windows do not overlap")

    @classmethod
    def from_aoa(
        cls,
        position_m: tuple[float, float],
        estimate: AoAEstimate,
        *,
        range_estimate: RangeEstimate | None = None,
    ) -> APObservation:
        provenance = estimate.provenance
        return cls(
            receiver_id=provenance.receiver_id,
            calibration_id=provenance.calibration_id,
            capture_manifest_id=provenance.capture_manifest_id,
            transmitter_address=provenance.transmitter_address,
            start_host_timestamp_ns=provenance.start_host_timestamp_ns,
            end_host_timestamp_ns=provenance.end_host_timestamp_ns,
            timebase_id=provenance.timebase_id,
            clock_uncertainty_ns=provenance.clock_uncertainty_ns,
            radio_config=provenance.radio_config,
            position_m=position_m,
            broadside_heading_deg=provenance.broadside_heading_deg,
            angle_grid_deg=estimate.angle_grid_deg,
            angle_normalized_support=estimate.normalized_support,
            evidence_score=estimate.evidence.score,
            front_back_ambiguous=("front_back_ambiguous" in estimate.evidence.flags),
            range_support_weights=(
                None if range_estimate is None else range_estimate.support_weights
            ),
            range_bands_m=(
                None
                if range_estimate is None
                else range_estimate.class_distance_bands_m
            ),
            range_evidence_score=(
                None if range_estimate is None else range_estimate.evidence.score
            ),
            range_evidence_flags=(
                () if range_estimate is None else range_estimate.evidence.flags
            ),
            range_model_id=(
                None if range_estimate is None else range_estimate.model_id
            ),
            range_device_id=(
                None if range_estimate is None else range_estimate.device_id
            ),
            range_room_id=(None if range_estimate is None else range_estimate.room_id),
            range_feature_provenance=(
                None if range_estimate is None else range_estimate.feature_provenance
            ),
        )


@dataclass
class GridSupport:
    x_m: np.ndarray
    y_m: np.ndarray
    fused_normalized_support: np.ndarray
    display_peak_position_m: tuple[float, float]
    display_covariance_m2: np.ndarray
    display_mass_radius_80_m: float
    evidence: Evidence

    def to_dict(self, *, include_grid: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "quantity": "multi_receiver_2d_fused_normalized_support",
            "display_peak_position_m": list(self.display_peak_position_m),
            "display_covariance_m2": self.display_covariance_m2.tolist(),
            "display_mass_radius_80_m": self.display_mass_radius_80_m,
            "evidence": {
                "score": self.evidence.score,
                "flags": list(self.evidence.flags),
                "details": self.evidence.details,
            },
        }
        if include_grid:
            result["grid"] = {
                "x_m": self.x_m.tolist(),
                "y_m": self.y_m.tolist(),
                "fused_normalized_support": self.fused_normalized_support.tolist(),
            }
        return result


def _angle_support(
    observation: APObservation, x_mesh: np.ndarray, y_mesh: np.ndarray
) -> np.ndarray:
    dx = x_mesh - observation.position_m[0]
    dy = y_mesh - observation.position_m[1]
    bearing = np.rad2deg(np.arctan2(dy, dx))
    relative = _wrap_degrees(bearing - observation.broadside_heading_deg)
    if observation.front_back_ambiguous:
        scan_angle = np.rad2deg(np.arcsin(np.sin(np.deg2rad(relative))))
        valid = np.ones(relative.shape, dtype=bool)
    else:
        scan_angle = relative
        valid = np.abs(relative) <= 90.0
    support = np.interp(
        scan_angle.ravel(),
        observation.angle_grid_deg,
        observation.angle_normalized_support,
        left=0.0,
        right=0.0,
    ).reshape(scan_angle.shape)
    support[~valid] = 0.0
    return support


def _range_support(
    observation: APObservation, x_mesh: np.ndarray, y_mesh: np.ndarray
) -> np.ndarray:
    if observation.range_support_weights is None or observation.range_bands_m is None:
        return np.ones(x_mesh.shape, dtype=float)
    distance = np.hypot(
        x_mesh - observation.position_m[0], y_mesh - observation.position_m[1]
    )
    support = np.zeros(distance.shape, dtype=float)
    for label, weight in observation.range_support_weights.items():
        lower, upper = observation.range_bands_m[label]
        mask = distance >= lower
        if np.isfinite(upper):
            mask &= distance < upper
        support[mask] += weight
    return support


def _validate_observations(
    observations: list[APObservation],
    *,
    minimum_receiver_separation_m: float,
    maximum_clock_uncertainty_ns: int,
    minimum_proven_overlap_ns: int,
) -> None:
    receiver_ids = [item.receiver_id for item in observations]
    calibration_ids = [item.calibration_id for item in observations]
    capture_manifest_ids = [item.capture_manifest_id for item in observations]
    if len(receiver_ids) != len(set(receiver_ids)):
        raise ValueError("receiver IDs must be unique")
    if len(calibration_ids) != len(set(calibration_ids)):
        raise ValueError("calibration IDs must be unique")
    if len(capture_manifest_ids) != len(set(capture_manifest_ids)):
        raise ValueError("capture manifest IDs must be unique")
    if len({item.transmitter_address for item in observations}) != 1:
        raise ValueError("all receivers must observe the same transmitter address")
    signatures = {item.radio_config.signature() for item in observations}
    if len(signatures) != 1:
        raise ValueError("receiver radio/tone configurations are incompatible")
    if len({item.timebase_id for item in observations}) != 1:
        raise ValueError("receivers do not share an explicit aligned timebase")
    if any(
        item.clock_uncertainty_ns > maximum_clock_uncertainty_ns
        for item in observations
    ):
        raise ValueError("receiver clock uncertainty exceeds the fusion gate")
    shared_start = max(
        item.start_host_timestamp_ns + item.clock_uncertainty_ns
        for item in observations
    )
    shared_end = min(
        item.end_host_timestamp_ns - item.clock_uncertainty_ns for item in observations
    )
    if shared_end - shared_start < minimum_proven_overlap_ns:
        raise ValueError("receiver windows lack overlap after clock uncertainty")
    for index, left in enumerate(observations):
        for right in observations[index + 1 :]:
            separation = float(
                np.linalg.norm(
                    np.asarray(left.position_m) - np.asarray(right.position_m)
                )
            )
            if separation < minimum_receiver_separation_m:
                raise ValueError("receiver coordinates are duplicate or too close")


def fuse_grid_support(
    observations: list[APObservation],
    *,
    room_bounds_m: tuple[float, float, float, float],
    room_id: str,
    grid_step_m: float = 0.10,
    prior_support: np.ndarray | None = None,
    minimum_evidence_score: float = 0.20,
    minimum_receiver_separation_m: float = 0.25,
    maximum_clock_uncertainty_ns: int = 1_000_000,
    minimum_proven_overlap_ns: int = 1_000_000,
    minimum_range_evidence_score: float = 0.35,
) -> GridSupport:
    """Fuse normalized display support; this is not Bayesian inference."""

    if not observations:
        raise ValueError("at least one receiver observation is required")
    if (
        not np.isfinite(grid_step_m)
        or grid_step_m <= 0
        or not np.isfinite(minimum_evidence_score)
        or not 0 <= minimum_evidence_score <= 1
        or not np.isfinite(minimum_receiver_separation_m)
        or minimum_receiver_separation_m <= 0
        or type(maximum_clock_uncertainty_ns) is not int
        or maximum_clock_uncertainty_ns < 0
        or type(minimum_proven_overlap_ns) is not int
        or minimum_proven_overlap_ns <= 0
        or not np.isfinite(minimum_range_evidence_score)
        or not 0 <= minimum_range_evidence_score <= 1
    ):
        raise ValueError("fusion thresholds are invalid")
    if type(room_id) is not str or not room_id.strip():
        raise ValueError("room_id is required")
    accepted = [
        item for item in observations if item.evidence_score >= minimum_evidence_score
    ]
    ignored_count = len(observations) - len(accepted)
    if not accepted:
        raise ValueError("all receiver observations have insufficient evidence")
    _validate_observations(
        accepted,
        minimum_receiver_separation_m=minimum_receiver_separation_m,
        maximum_clock_uncertainty_ns=maximum_clock_uncertainty_ns,
        minimum_proven_overlap_ns=minimum_proven_overlap_ns,
    )

    x_min, x_max, y_min, y_max = room_bounds_m
    if not np.all(np.isfinite(room_bounds_m)) or not x_min < x_max or not y_min < y_max:
        raise ValueError("room bounds are invalid")
    x_values = np.arange(x_min, x_max + grid_step_m / 2.0, grid_step_m)
    y_values = np.arange(y_min, y_max + grid_step_m / 2.0, grid_step_m)
    x_mesh, y_mesh = np.meshgrid(x_values, y_values)
    if prior_support is None:
        log_support = np.zeros(x_mesh.shape, dtype=float)
    else:
        prior_support = np.asarray(prior_support, dtype=float)
        if (
            prior_support.shape != x_mesh.shape
            or np.any(~np.isfinite(prior_support))
            or np.any(prior_support < 0)
            or prior_support.sum() <= 0
        ):
            raise ValueError("prior support has the wrong shape or invalid values")
        log_support = np.log(np.maximum(prior_support / prior_support.sum(), 1e-15))

    disqualifying_range_flags = {
        "unseen_transmitter_device",
        "room_domain_shift",
        "out_of_distribution",
    }
    range_used: list[APObservation] = []
    range_ignored: list[APObservation] = []
    for observation in accepted:
        angle_support = _angle_support(observation, x_mesh, y_mesh)
        log_support += observation.evidence_score * np.log(
            np.maximum(angle_support, 1e-15)
        )
        if observation.range_support_weights is None:
            continue
        range_is_usable = (
            observation.range_evidence_score >= minimum_range_evidence_score
            and observation.range_room_id == room_id
            and not disqualifying_range_flags.intersection(
                observation.range_evidence_flags
            )
        )
        if not range_is_usable:
            range_ignored.append(observation)
            continue
        log_support += observation.range_evidence_score * np.log(
            np.maximum(_range_support(observation, x_mesh, y_mesh), 1e-15)
        )
        range_used.append(observation)

    log_support -= np.max(log_support)
    fused = np.exp(log_support)
    fused /= fused.sum()
    maximum = np.unravel_index(int(np.argmax(fused)), fused.shape)
    peak_position = (float(x_mesh[maximum]), float(y_mesh[maximum]))
    mean_x = float(np.sum(fused * x_mesh))
    mean_y = float(np.sum(fused * y_mesh))
    delta_x = x_mesh - mean_x
    delta_y = y_mesh - mean_y
    covariance = np.asarray(
        [
            [np.sum(fused * delta_x**2), np.sum(fused * delta_x * delta_y)],
            [np.sum(fused * delta_x * delta_y), np.sum(fused * delta_y**2)],
        ],
        dtype=float,
    )
    radial = np.hypot(x_mesh - peak_position[0], y_mesh - peak_position[1]).ravel()
    order = np.argsort(radial, kind="stable")
    cumulative = np.cumsum(fused.ravel()[order])
    mass_index = int(np.searchsorted(cumulative, 0.8, side="left"))
    mass_radius = float(radial[order[min(mass_index, order.size - 1)]])

    entropy = normalized_entropy(fused.ravel())
    mean_quality = float(np.mean([item.evidence_score for item in accepted]))
    geometry_factor = min(1.0, len(accepted) / 2.0)
    score = float(
        np.clip(
            mean_quality * geometry_factor * (0.35 + 0.65 * (1.0 - entropy)),
            0.0,
            1.0,
        )
    )
    flags = ["normalized_support_heatmap", "not_bayesian_inference"]
    if ignored_count:
        flags.append("low_evidence_receivers_ignored")
    if len(accepted) == 1:
        flags.extend(("single_receiver_underdetermined", "front_back_not_resolved"))
    else:
        flags.append("multi_receiver_geometry")
    if any(item.front_back_ambiguous for item in accepted):
        flags.append("front_back_components_retained")
    if range_used:
        flags.extend(("range_proxy_used", "not_absolute_range"))
    if range_ignored:
        flags.append("low_or_shifted_range_proxy_ignored")
    for item in (*range_used, *range_ignored):
        flags.extend(f"range:{flag}" for flag in item.range_evidence_flags)
    if mass_radius > 2.0:
        flags.append("diffuse_display_support")

    return GridSupport(
        x_m=x_values,
        y_m=y_values,
        fused_normalized_support=fused,
        display_peak_position_m=peak_position,
        display_covariance_m2=covariance,
        display_mass_radius_80_m=mass_radius,
        evidence=Evidence(
            score=score,
            flags=tuple(dict.fromkeys(flags)),
            details={
                "receiver_count": len(accepted),
                "ignored_receiver_count": ignored_count,
                "grid_step_m": grid_step_m,
                "support_entropy": entropy,
                "mean_observation_quality": mean_quality,
                "timebase_id": accepted[0].timebase_id,
                "maximum_clock_uncertainty_ns": max(
                    item.clock_uncertainty_ns for item in accepted
                ),
                "minimum_proven_overlap_ns": minimum_proven_overlap_ns,
                "range_receiver_count_used": len(range_used),
                "range_receiver_count_ignored": len(range_ignored),
                "range_model_ids_used": [item.range_model_id for item in range_used],
                "display_mass_has_no_calibrated_coverage_semantics": True,
                "confidence_is_not_ground_truth_accuracy": True,
            },
        ),
    )
