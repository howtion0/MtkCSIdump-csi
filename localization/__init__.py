"""Honest, evidence-gated coarse localization for AX3000T CSI captures.

The public API deliberately uses words such as ``coarse`` and ``proxy``.  A
single two-element array cannot turn ordinary fixed-channel CSI into absolute
time-of-flight range or an unambiguous 360-degree bearing.
"""

from .aoa import AoAEstimate, estimate_coarse_aoa
from .calibration import ChainCalibration, estimate_chain_calibration
from .csi2 import CSI2ProtocolError, decode_csi2_datagram
from .fusion import APObservation, GridSupport, fuse_grid_support
from .grouping import GroupedPPDU, StreamKey, group_same_ppdu
from .models import CSIRecord, Evidence
from .range_proxy import (
    BoundRangeFeatures,
    KNNRangeProxy,
    RangeEstimate,
    RangeFeatureProvenance,
    extract_bound_range_features,
)

__all__ = [
    "APObservation",
    "AoAEstimate",
    "BoundRangeFeatures",
    "CSI2ProtocolError",
    "CSIRecord",
    "ChainCalibration",
    "Evidence",
    "GridSupport",
    "GroupedPPDU",
    "KNNRangeProxy",
    "RangeEstimate",
    "RangeFeatureProvenance",
    "StreamKey",
    "decode_csi2_datagram",
    "estimate_chain_calibration",
    "estimate_coarse_aoa",
    "extract_bound_range_features",
    "fuse_grid_support",
    "group_same_ppdu",
]
