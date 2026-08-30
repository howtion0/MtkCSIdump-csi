from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from localization.csi2 import CSI_PRESENT_PKT_SN, CSI_PRESENT_TR_STREAM
from localization.grouping import StreamKey, group_same_ppdu
from tests.helpers import ALL_PRESENCE, make_record


def _pair(**common: object) -> list:
    return [
        make_record(rx_idx=0, sequence=1, **common),
        make_record(rx_idx=1, sequence=2, **common),
    ]


def test_stream_identity_keeps_tx_and_transport_stream_without_rx_collapse() -> None:
    records = _pair(tx_idx=0, transport_stream=0)
    records += _pair(tx_idx=1, transport_stream=1)
    group = group_same_ppdu(records)[0]
    assert len(group.records_by_stream) == 4
    assert StreamKey(0, 0, 0) in group.records_by_stream
    assert StreamKey(1, 0, 1) in group.records_by_stream
    with pytest.raises(ValueError, match="ambiguous Tx/transport"):
        group.pair(0, 1)
    left, right = group.pair(0, 1, tx_idx=1, transport_stream=1)
    assert left.tx_idx == right.tx_idx == 1
    assert left.transport_stream == right.transport_stream == 1


def test_equivalent_duplicate_deduplicates_but_conflict_hard_rejects() -> None:
    records = _pair()
    duplicate = replace(records[0], sequence=99, host_timestamp_ns=1_000_000_010)
    group = group_same_ppdu([*records, duplicate])[0]
    assert len(group.records_by_stream) == 2
    assert "equivalent_duplicate_stream_deduplicated" in group.evidence.flags
    assert group.pair(0, 1)

    conflict = replace(
        duplicate,
        samples=duplicate.samples * np.exp(0.2j),
    )
    bad = group_same_ppdu([*records, conflict])[0]
    assert "conflicting_duplicate_stream" in bad.hard_failure_flags
    with pytest.raises(ValueError, match="hard evidence gates"):
        bad.pair(0, 1, allow_low_confidence_identity=True)


def test_allow_low_identity_never_bypasses_metadata_or_tone_hard_gate() -> None:
    weak_presence = ALL_PRESENCE & ~CSI_PRESENT_PKT_SN
    weak = _pair(presence_flags=weak_presence)
    group = group_same_ppdu(weak)[0]
    assert group.identity_mode == "fallback"
    assert group.pair(0, 1, allow_low_confidence_identity=True)
    with pytest.raises(ValueError, match="same-PPDU identity"):
        group.pair(0, 1)

    mismatched = [
        make_record(rx_idx=0),
        make_record(rx_idx=1, frequency_mhz=5520, sequence=2),
    ]
    hard = group_same_ppdu(mismatched)[0]
    assert any("channel_frequency_mhz" in flag for flag in hard.hard_failure_flags)
    with pytest.raises(ValueError, match="hard evidence gates"):
        hard.pair(0, 1, allow_low_confidence_identity=True)


def test_strict_identity_wrap_or_reuse_splits_on_host_epoch() -> None:
    first = _pair(host_timestamp_ns=1_000_000_000)
    second = [
        replace(record, host_timestamp_ns=1_010_000_000, sequence=record.sequence + 100)
        for record in _pair()
    ]
    groups = group_same_ppdu([*first, *second], strict_identity_window_ns=1_000_000)
    assert len(groups) == 2
    assert "reused_identity_after_host_gap" in groups[1].evidence.flags


def test_stage2_80mhz_final_segment_is_never_concatenated_again() -> None:
    samples = np.arange(256, dtype=float) + 1j
    records = _pair(
        channel_bw_enum=2,
        data_bw_enum=2,
        samples=samples,
        segment_number=7,
        remain_last=0,
    )
    group = group_same_ppdu(records)[0]
    left, right = group.pair(0, 1)
    assert left.sample_count == right.sample_count == 256
    assert np.array_equal(left.samples, samples)


def test_selector_must_match_one_common_tx_and_stream() -> None:
    group = group_same_ppdu(
        [
            make_record(rx_idx=0, tx_idx=0, transport_stream=0),
            make_record(rx_idx=1, tx_idx=1, transport_stream=0, sequence=2),
        ]
    )[0]
    with pytest.raises(KeyError, match="common Tx/stream"):
        group.pair(0, 1, allow_low_confidence_identity=True)


def test_mapping_can_explicitly_select_absent_transport_stream() -> None:
    presence = ALL_PRESENCE & ~CSI_PRESENT_TR_STREAM
    group = group_same_ppdu(_pair(presence_flags=presence))[0]
    left, right = group.pair(0, 1, require_transport_stream_absent=True)
    assert left.transport_stream is right.transport_stream is None
