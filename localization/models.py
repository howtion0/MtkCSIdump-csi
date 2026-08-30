"""Shared, dependency-light data models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Evidence:
    """Machine-readable evidence quality attached to every inference.

    ``score`` is a quality indicator in [0, 1], not a calibrated probability
    that the claimed physical quantity is correct.
    """

    score: float
    flags: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not np.isfinite(self.score) or not 0.0 <= self.score <= 1.0:
            raise ValueError("evidence score must be finite and in [0, 1]")

    def with_flags(self, *flags: str, score: float | None = None) -> Evidence:
        merged = tuple(dict.fromkeys((*self.flags, *flags)))
        return Evidence(
            self.score if score is None else score, merged, dict(self.details)
        )


@dataclass
class CSIRecord:
    """One decoded CSI2 observation for one receive chain or one segment."""

    sequence: int
    host_timestamp_ns: int
    driver_timestamp: int
    transmitter_address: str
    band: int
    channel_frequency_mhz: int
    channel_bandwidth: int
    rx_idx: int
    tx_idx: int
    samples: np.ndarray
    quality_flags: int = 0
    presence_flags: int = 0
    packet_sequence_number: int | None = None
    segment_number: int | None = None
    remain_last: int | None = None
    transport_stream: int | None = None
    h_idx: int | None = None
    chain_info: int | None = None
    # These bytes are driver/firmware telemetry.  Stage 3 deliberately keeps
    # the host names unit-free: no calibration in this project proves that
    # every firmware build reports physical dBm or dB.
    rssi_raw: int | None = None
    snr_raw: int | None = None
    data_bandwidth: int | None = None
    primary_channel_index: int | None = None
    rx_mode: int | None = None
    rate_mcs: int | None = None
    rate_nss: int | None = None
    rate_guard_interval: int | None = None
    rate_kbps: int | None = None
    ext_info: int = 0
    protocol_version: int = 2

    def __post_init__(self) -> None:
        samples = np.asarray(self.samples, dtype=np.complex128)
        if samples.ndim != 1 or samples.size == 0:
            raise ValueError("CSI samples must be a non-empty one-dimensional array")
        if not np.all(np.isfinite(samples.real)) or not np.all(
            np.isfinite(samples.imag)
        ):
            raise ValueError("CSI samples contain non-finite values")
        self.samples = samples
        self.transmitter_address = normalize_mac(self.transmitter_address)

    @property
    def sample_count(self) -> int:
        return int(self.samples.size)

    @property
    def channel_bandwidth_mhz(self) -> int:
        # Local import avoids a models -> csi2 -> models cycle at import time.
        from .csi2 import bandwidth_enum_to_mhz

        return bandwidth_enum_to_mhz(self.channel_bandwidth)

    @property
    def data_bandwidth_mhz(self) -> int | None:
        if self.data_bandwidth is None:
            return None
        from .csi2 import bandwidth_enum_to_mhz

        return bandwidth_enum_to_mhz(self.data_bandwidth)

    def has(self, presence_bit: int, value: Any) -> bool:
        return bool(self.presence_flags & presence_bit) and value is not None


def normalize_mac(value: str | bytes | bytearray | Iterable[int]) -> str:
    if isinstance(value, str):
        compact = value.replace(":", "").replace("-", "").lower()
        if len(compact) != 12:
            raise ValueError("MAC address must contain six octets")
        try:
            raw = bytes.fromhex(compact)
        except ValueError as exc:
            raise ValueError("invalid MAC address") from exc
    else:
        raw = bytes(value)
        if len(raw) != 6:
            raise ValueError("MAC address must contain six octets")
    return ":".join(f"{octet:02x}" for octet in raw)


def softmax(values: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        raise ValueError("softmax requires at least one value")
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    shifted = values / temperature - np.nanmax(values / temperature)
    exp_values = np.exp(np.clip(shifted, -700.0, 0.0))
    total = float(exp_values.sum())
    if not np.isfinite(total) or total <= 0:
        return np.full(values.shape, 1.0 / values.size)
    return exp_values / total


def normalized_entropy(weights: np.ndarray) -> float:
    weights = np.asarray(weights, dtype=float)
    weights = weights[weights > 0]
    if weights.size <= 1:
        return 0.0
    entropy = -float(np.sum(weights * np.log(weights)))
    return entropy / float(np.log(weights.size))
