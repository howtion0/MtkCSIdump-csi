#!/usr/bin/env python3
"""Hardware-free verification for the AX3000T mt7915 CSI v2 patch."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


STAGE_DIR = Path(__file__).resolve().parents[1]
PATCH = STAGE_DIR / "patches" / "0001-mt7915-csi-v2-hardened.patch"
EXPECTED_COMMIT = "39c960c3ada558b4c2e7915772483d3731573d09"

FW_VER = 0
CBW = 1
RSSI = 2
SNR = 3
BAND = 4
CSI_NUM = 5
I_DATA = 6
Q_DATA = 7
DBW = 8
CH_IDX = 9
TA = 10
EXTRA_INFO = 11
RX_MODE = 12
CHAIN_INFO = 17
TX_RX_IDX = 18
TS = 19
PKT_SN = 20
BW_SEG = 21
REMAIN_LAST = 22
TR_STREAM = 23


def run(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip()


def tlv(tag: int, payload: bytes) -> bytes:
    return struct.pack("<II", tag, len(payload)) + payload


def scalar(tag: int, value: int) -> bytes:
    return tlv(tag, struct.pack("<I", value))


def make_record(
    bandwidth: int,
    vector_slots: int,
    reported_count: int | None = None,
    include_optional: bool = True,
    packet_sn: int = 77,
    segment_num: int = 0,
    remain_last: int = 0,
    chain_info: int = 0x8001,
    band: int = 1,
    ta: bytes = bytes.fromhex("021122334455"),
    sample_start: int = 0,
) -> bytes:
    expected = 64 << bandwidth
    count = expected if reported_count is None else reported_count
    samples = range(sample_start, sample_start + vector_slots)
    vector = struct.pack("<" + "h" * vector_slots, *samples)
    parts = [
        scalar(FW_VER, 1),
        scalar(CBW, bandwidth),
        scalar(RSSI, 0xFFFFFFD6),
        scalar(SNR, 31),
        scalar(BAND, band),
        scalar(CSI_NUM, count),
        tlv(I_DATA, vector),
        tlv(Q_DATA, vector),
        scalar(DBW, bandwidth),
        scalar(CH_IDX, 0),
        tlv(TA, ta + b"\x00\x00"),
        scalar(EXTRA_INFO, 0),
        scalar(RX_MODE, 4),
        scalar(CHAIN_INFO, chain_info),
        scalar(TX_RX_IDX, 0x00010002),
        scalar(TS, 123456),
    ]
    if include_optional:
        parts.extend(
            [
                scalar(PKT_SN, packet_sn),
                scalar(BW_SEG, segment_num),
                scalar(REMAIN_LAST, remain_last),
                scalar(TR_STREAM, 1),
            ]
        )
    return b"".join(parts)


class InvalidRecord(ValueError):
    pass


def parse_fields(raw: bytes) -> dict[int, bytes]:
    offset = 0
    fields: dict[int, bytes] = {}
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise InvalidRecord("truncated TLV header")
        tag, length = struct.unpack_from("<II", raw, offset)
        offset += 8
        if length > len(raw) - offset:
            raise InvalidRecord("truncated TLV payload")
        payload = raw[offset : offset + length]
        offset += length
        if tag <= TR_STREAM and tag in fields:
            raise InvalidRecord("duplicate known tag")
        fields[tag] = payload
    return fields


@dataclass
class ModelPart:
    bandwidth: int
    data_bandwidth: int
    band: int
    data_num: int
    data_i: list[int]
    data_q: list[int]
    ta: bytes
    chain_info: int
    tx_rx_idx: int
    rx_mode: int
    packet_sn: int | None
    segment_num: int | None
    remain_last: int | None
    tr_stream: int | None


def scalar_value(fields: dict[int, bytes], tag: int) -> int:
    return struct.unpack("<I", fields[tag])[0]


def decode_model(raw: bytes) -> ModelPart:
    """Executable model of the parser and per-event count invariants."""
    fields = parse_fields(raw)
    required = {
        CBW, BAND, CSI_NUM, I_DATA, Q_DATA, DBW, TA,
        CHAIN_INFO, TX_RX_IDX, TS,
    }
    if not required.issubset(fields):
        raise InvalidRecord("missing required tag")

    scalar_tags = {
        FW_VER, CBW, RSSI, SNR, BAND, CSI_NUM, DBW, CH_IDX,
        EXTRA_INFO, RX_MODE, CHAIN_INFO, TX_RX_IDX, TS, PKT_SN,
        BW_SEG, REMAIN_LAST, TR_STREAM,
    }
    for tag in scalar_tags.intersection(fields):
        if len(fields[tag]) != 4:
            raise InvalidRecord("bad scalar length")

    bandwidth = scalar_value(fields, CBW)
    data_bandwidth = scalar_value(fields, DBW)
    band = scalar_value(fields, BAND)
    reported_count = scalar_value(fields, CSI_NUM)
    if bandwidth > 2 or data_bandwidth > bandwidth or band > 1:
        raise InvalidRecord("invalid enum")
    if not 0 < reported_count <= 256:
        raise InvalidRecord("invalid reported count")
    if not 6 <= len(fields[TA]) <= 8:
        raise InvalidRecord("invalid transmitter address")

    has_segment = BW_SEG in fields
    if has_segment != (REMAIN_LAST in fields):
        raise InvalidRecord("incomplete segmentation metadata")

    packet_sn = scalar_value(fields, PKT_SN) if PKT_SN in fields else None
    segment_num = scalar_value(fields, BW_SEG) if has_segment else None
    remain_last = scalar_value(fields, REMAIN_LAST) if has_segment else None
    tr_stream = scalar_value(fields, TR_STREAM) if TR_STREAM in fields else None
    if packet_sn is not None and packet_sn > 0xFFFF:
        raise InvalidRecord("packet sequence exceeds u16 ABI")
    if remain_last is not None and remain_last > 1:
        raise InvalidRecord("invalid remain-last")
    if tr_stream is not None and tr_stream > 0xFF:
        raise InvalidRecord("invalid stream")

    i_len = len(fields[I_DATA])
    q_len = len(fields[Q_DATA])
    if not i_len or i_len != q_len or i_len % 2 or i_len > 512:
        raise InvalidRecord("invalid vector length")

    available = i_len // 2
    expected = 64 << bandwidth
    if bandwidth == 2 and has_segment:
        if reported_count > available:
            raise InvalidRecord("segment count exceeds vector")
        effective = reported_count
    else:
        if available < expected:
            raise InvalidRecord("short vector")
        if reported_count not in (expected, available):
            raise InvalidRecord("count contradicts vector")
        effective = expected

    vector_format = "<" + "h" * available
    values_i = list(struct.unpack(vector_format, fields[I_DATA]))[:effective]
    values_q = list(struct.unpack(vector_format, fields[Q_DATA]))[:effective]
    return ModelPart(
        bandwidth=bandwidth,
        data_bandwidth=data_bandwidth,
        band=band,
        data_num=effective,
        data_i=values_i,
        data_q=values_q,
        ta=fields[TA][:6],
        chain_info=scalar_value(fields, CHAIN_INFO),
        tx_rx_idx=scalar_value(fields, TX_RX_IDX),
        rx_mode=scalar_value(fields, RX_MODE) & 0xFFFF,
        packet_sn=packet_sn,
        segment_num=segment_num,
        remain_last=remain_last,
        tr_stream=tr_stream,
    )


def parse_model(raw: bytes) -> int:
    return decode_model(raw).data_num


class SegmentAssembler:
    """Reference model for the driver's single in-flight buffer per PHY."""

    def __init__(self) -> None:
        self.buffer: ModelPart | None = None
        self.malformed = 0

    @staticmethod
    def same_series(first: ModelPart, part: ModelPart) -> bool:
        return (
            first.packet_sn is not None
            and part.packet_sn is not None
            and first.packet_sn == part.packet_sn
            and first.chain_info == part.chain_info
            and first.ta == part.ta
            and first.band == part.band
            and first.tx_rx_idx == part.tx_rx_idx
            and first.bandwidth == part.bandwidth
            and first.data_bandwidth == part.data_bandwidth
            and first.rx_mode == part.rx_mode
            and first.tr_stream == part.tr_stream
            and first.segment_num is not None
            and part.segment_num == first.segment_num + 1
        )

    def reject(self) -> None:
        self.buffer = None
        self.malformed += 1

    def feed(self, part: ModelPart) -> ModelPart | None:
        has_segment = part.segment_num is not None
        if self.buffer is not None and (
            part.bandwidth != 2 or not has_segment or part.segment_num == 0
        ):
            self.reject()

        if part.bandwidth != 2:
            if (part.segment_num not in (None, 0)) or part.remain_last not in (None, 0):
                self.reject()
                return None
            return part

        if not has_segment:
            if part.data_num != 256:
                self.reject()
                return None
            return part

        if part.segment_num == 0:
            if part.remain_last == 0:
                if part.data_num != 256:
                    self.reject()
                    return None
                return part
            if part.packet_sn is None or part.data_num >= 256:
                self.reject()
                return None
            self.buffer = part
            return None

        first = self.buffer
        if (
            first is None
            or not self.same_series(first, part)
            or first.data_num + part.data_num > 256
        ):
            self.reject()
            return None

        first.data_i.extend(part.data_i)
        first.data_q.extend(part.data_q)
        first.data_num += part.data_num
        first.segment_num = part.segment_num
        first.remain_last = part.remain_last
        if part.remain_last:
            if first.data_num >= 256:
                self.reject()
            return None
        if first.data_num != 256:
            self.reject()
            return None

        self.buffer = None
        return first


class TlvModelTests(unittest.TestCase):
    def test_20mhz_trimmed(self) -> None:
        self.assertEqual(parse_model(make_record(0, 64)), 64)

    def test_20mhz_padded_firmware_variant(self) -> None:
        self.assertEqual(parse_model(make_record(0, 256, 256)), 64)

    def test_40mhz_padded_firmware_variant(self) -> None:
        self.assertEqual(parse_model(make_record(1, 256, 128)), 128)

    def test_80mhz(self) -> None:
        self.assertEqual(parse_model(make_record(2, 256)), 256)

    def test_old_firmware_without_optional_metadata(self) -> None:
        raw = make_record(0, 64, include_optional=False)
        self.assertEqual(parse_model(raw), 64)

    def test_truncated_payload_is_rejected(self) -> None:
        with self.assertRaises(InvalidRecord):
            parse_model(make_record(0, 64)[:-1])

    def test_duplicate_known_tag_is_rejected(self) -> None:
        with self.assertRaises(InvalidRecord):
            parse_model(make_record(0, 64) + scalar(TS, 9))

    def test_contradictory_count_is_rejected(self) -> None:
        with self.assertRaises(InvalidRecord):
            parse_model(make_record(0, 256, 65))

    def test_short_vector_is_rejected(self) -> None:
        with self.assertRaises(InvalidRecord):
            parse_model(make_record(1, 64, 64))

    def test_future_tag_is_safely_skipped(self) -> None:
        raw = make_record(0, 64) + tlv(31, b"future")
        self.assertEqual(parse_model(raw), 64)

    def test_160mhz_is_explicitly_rejected(self) -> None:
        with self.assertRaises(InvalidRecord):
            parse_model(make_record(3, 256, 256))


def segment_part(
    count: int,
    number: int,
    remain: int,
    packet: int = 77,
    chain: int = 0x8001,
    band: int = 1,
    ta: bytes = bytes.fromhex("021122334455"),
    sample_start: int = 0,
) -> ModelPart:
    raw = make_record(
        2,
        256,
        reported_count=count,
        packet_sn=packet,
        segment_num=number,
        remain_last=remain,
        chain_info=chain,
        band=band,
        ta=ta,
        sample_start=sample_start,
    )
    return decode_model(raw)


class SegmentAssemblerTests(unittest.TestCase):
    def test_first_last(self) -> None:
        assembler = SegmentAssembler()
        self.assertIsNone(assembler.feed(segment_part(128, 0, 1)))
        result = assembler.feed(segment_part(128, 1, 0, sample_start=1000))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.data_num, 256)
        self.assertEqual(result.segment_num, 1)
        self.assertEqual(result.data_i[:3], [0, 1, 2])
        self.assertEqual(result.data_i[128:131], [1000, 1001, 1002])
        self.assertEqual(assembler.malformed, 0)

    def test_first_middle_last(self) -> None:
        assembler = SegmentAssembler()
        self.assertIsNone(assembler.feed(segment_part(64, 0, 1)))
        self.assertIsNone(
            assembler.feed(segment_part(64, 1, 1, sample_start=1000))
        )
        result = assembler.feed(segment_part(128, 2, 0, sample_start=2000))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.data_num, 256)
        self.assertEqual(result.segment_num, 2)
        self.assertEqual(result.data_i[64], 1000)
        self.assertEqual(result.data_i[128], 2000)

    def test_out_of_order_segment_clears_buffer(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(64, 0, 1))
        self.assertIsNone(assembler.feed(segment_part(192, 2, 0)))
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, 1)

    def test_cross_packet_is_rejected(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(128, 0, 1, packet=10))
        self.assertIsNone(assembler.feed(segment_part(128, 1, 0, packet=11)))
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, 1)

    def test_cross_chain_is_rejected(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(128, 0, 1, chain=0x8001))
        self.assertIsNone(
            assembler.feed(segment_part(128, 1, 0, chain=0x8002))
        )
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, 1)

    def test_cross_band_is_rejected(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(128, 0, 1, band=0))
        self.assertIsNone(assembler.feed(segment_part(128, 1, 0, band=1)))
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, 1)

    def test_cross_transmitter_is_rejected(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(128, 0, 1))
        other = bytes.fromhex("02aabbccddee")
        self.assertIsNone(assembler.feed(segment_part(128, 1, 0, ta=other)))
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, 1)

    def test_overflow_is_rejected(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(200, 0, 1))
        malformed_before = assembler.malformed
        self.assertIsNone(assembler.feed(segment_part(100, 1, 0)))
        self.assertIsNone(assembler.buffer)
        self.assertEqual(assembler.malformed, malformed_before + 1)

        underfilled = SegmentAssembler()
        underfilled.feed(segment_part(128, 0, 1))
        malformed_before = underfilled.malformed
        self.assertIsNone(underfilled.feed(segment_part(64, 1, 0)))
        self.assertIsNone(underfilled.buffer)
        self.assertEqual(underfilled.malformed, malformed_before + 1)

    def test_missing_segment_then_new_first_recovers(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(64, 0, 1, packet=10))
        assembler.feed(segment_part(192, 2, 0, packet=10))
        self.assertEqual(assembler.malformed, 1)

        assembler.feed(segment_part(128, 0, 1, packet=11))
        result = assembler.feed(segment_part(128, 1, 0, packet=11))
        self.assertIsNotNone(result)
        self.assertEqual(assembler.malformed, 1)

    def test_new_first_discards_stale_sequence_and_recovers(self) -> None:
        assembler = SegmentAssembler()
        assembler.feed(segment_part(128, 0, 1, packet=10))
        assembler.feed(segment_part(128, 0, 1, packet=11))
        result = assembler.feed(segment_part(128, 1, 0, packet=11))
        self.assertIsNotNone(result)
        self.assertEqual(assembler.malformed, 1)


EXPECTED_ABI = {
    "MTK_VENDOR_ATTR_CSI_DATA_UNSPEC": 0,
    "MTK_VENDOR_ATTR_CSI_DATA_PAD": 1,
    "MTK_VENDOR_ATTR_CSI_DATA_VER": 2,
    "MTK_VENDOR_ATTR_CSI_DATA_TS": 3,
    "MTK_VENDOR_ATTR_CSI_DATA_RSSI": 4,
    "MTK_VENDOR_ATTR_CSI_DATA_SNR": 5,
    "MTK_VENDOR_ATTR_CSI_DATA_BW": 6,
    "MTK_VENDOR_ATTR_CSI_DATA_CH_IDX": 7,
    "MTK_VENDOR_ATTR_CSI_DATA_TA": 8,
    "MTK_VENDOR_ATTR_CSI_DATA_I": 9,
    "MTK_VENDOR_ATTR_CSI_DATA_Q": 10,
    "MTK_VENDOR_ATTR_CSI_DATA_INFO": 11,
    "MTK_VENDOR_ATTR_CSI_DATA_RSVD1": 12,
    "MTK_VENDOR_ATTR_CSI_DATA_RSVD2": 13,
    "MTK_VENDOR_ATTR_CSI_DATA_RSVD3": 14,
    "MTK_VENDOR_ATTR_CSI_DATA_RSVD4": 15,
    "MTK_VENDOR_ATTR_CSI_DATA_TX_ANT": 16,
    "MTK_VENDOR_ATTR_CSI_DATA_RX_ANT": 17,
    "MTK_VENDOR_ATTR_CSI_DATA_MODE": 18,
    "MTK_VENDOR_ATTR_CSI_DATA_H_IDX": 19,
    "MTK_VENDOR_ATTR_CSI_DATA_CH_BW": 20,
    "MTK_VENDOR_ATTR_CSI_DATA_NUM": 21,
    "MTK_VENDOR_ATTR_CSI_DATA_PKT_SN": 22,
    "MTK_VENDOR_ATTR_CSI_DATA_SEGMENT_NUM": 23,
    "MTK_VENDOR_ATTR_CSI_DATA_REMAIN_LAST": 24,
    "MTK_VENDOR_ATTR_CSI_DATA_TR_STREAM": 25,
    "MTK_VENDOR_ATTR_CSI_DATA_CHAIN_INFO": 26,
    "MTK_VENDOR_ATTR_CSI_DATA_BAND": 27,
}


def parse_abi(header: str) -> dict[str, int]:
    body = header.split("enum mtk_vendor_attr_csi_data {", 1)[1]
    body = body.split("/* keep last */", 1)[0]
    result: dict[str, int] = {}
    value = -1
    for raw_line in body.splitlines():
        line = raw_line.split("/*", 1)[0].strip().rstrip(",")
        if not line.startswith("MTK_VENDOR_ATTR_CSI_DATA_"):
            continue
        name = line.split("=", 1)[0].strip()
        value += 1
        result[name] = value
    return result


def verify_patch(baseline: Path) -> None:
    if not PATCH.is_file():
        raise SystemExit(f"missing patch: {PATCH}")
    baseline = baseline.resolve()
    if not (baseline / ".git").exists():
        raise SystemExit(f"missing mt76 baseline: {baseline}")

    commit = run("git", "rev-parse", "HEAD", cwd=baseline)
    if commit != EXPECTED_COMMIT:
        raise SystemExit(
            f"wrong mt76 baseline: {commit}; expected {EXPECTED_COMMIT}"
        )

    run("git", "apply", "--check", str(PATCH), cwd=baseline)
    with tempfile.TemporaryDirectory(prefix="mt7915-csi-v2-") as tmp:
        work = Path(tmp) / "mt76"
        # The authoritative checkout may be a partial clone with intentionally
        # missing history, so copy the materialized tree instead of cloning it.
        shutil.copytree(
            baseline, work, ignore=shutil.ignore_patterns(".git")
        )
        run("git", "init", "--quiet", cwd=work)
        run("git", "add", ".", cwd=work)
        run("git", "apply", str(PATCH), cwd=work)
        run("git", "diff", "--check", cwd=work)

        vendor_h = (work / "mt7915/vendor.h").read_text()
        actual_abi = parse_abi(vendor_h)
        if actual_abi != EXPECTED_ABI:
            raise AssertionError(f"ABI drift:\n{actual_abi}")

        vendor_c = (work / "mt7915/vendor.c").read_text()
        mcu_c = (work / "mt7915/mcu.c").read_text()
        mt7915_h = (work / "mt7915/mt7915.h").read_text()
        if "mac_addr[idx++]" in vendor_c:
            raise AssertionError("unsafe sequential MAC indexing returned")
        if "count * sizeof(csi->data_i[0])" not in vendor_c:
            raise AssertionError("I-vector memmove byte scaling is missing")
        if "count * sizeof(csi->data_q[0])" not in vendor_c:
            raise AssertionError("Q-vector memmove byte scaling is missing")
        if "tlv_len > len - sizeof(*tlv)" not in mcu_c:
            raise AssertionError("TLV remaining-length guard is missing")
        if "data_i_len != data_q_len" not in mcu_c:
            raise AssertionError("I/Q length equality guard is missing")
        if "sizeof(req), true" not in mcu_c:
            raise AssertionError("CSI MCU command is not acknowledged")
        if "nla_put_u32(skb, MTK_VENDOR_ATTR_CSI_DATA_NUM" not in vendor_c:
            raise AssertionError("DATA_NUM ABI type drifted away from u32")
        if "nla_put_u16(skb, MTK_VENDOR_ATTR_CSI_DATA_PKT_SN" not in vendor_c:
            raise AssertionError("PKT_SN ABI type drifted away from u16")
        if "nla_put_u32(skb, MTK_VENDOR_ATTR_CSI_DATA_SEGMENT_NUM" not in vendor_c:
            raise AssertionError("SEGMENT_NUM ABI type drifted away from u32")
        if "MT7915_CSI_VALID_PKT_SN" not in vendor_c:
            raise AssertionError("optional metadata presence is not preserved")
        if "u32 segment_num;" not in mt7915_h:
            raise AssertionError("segment_num was truncated below u32")
        if "mt7915_csi_reassemble" not in mcu_c:
            raise AssertionError("80 MHz segment state machine is missing")
        if "CSI_BW80_DATA_COUNT - first->data_num" not in mcu_c:
            raise AssertionError("segment accumulation bound is missing")
        reassemble = mcu_c.split("mt7915_csi_reassemble", 1)[1]
        reassemble = reassemble.split(
            "static int\nmt7915_mcu_report_csi", 1
        )[0]
        malformed_paths = {
            "malformed_sequence": reassemble.split(
                "malformed_sequence:", 1
            )[1].split("malformed_buffer:", 1)[0],
            "malformed_buffer": reassemble.split(
                "malformed_buffer:", 1
            )[1].split("malformed:", 1)[0],
            "malformed": reassemble.split("malformed:", 1)[1],
        }
        for label, block in malformed_paths.items():
            if block.count("phy->csi.malformed++;") != 1:
                raise AssertionError(
                    f"{label} must count each rejected event exactly once"
                )
        for identity_guard in (
            "first->pkt_sn != next->pkt_sn",
            "first->chain_info != next->chain_info",
            "ether_addr_equal(first->ta, next->ta)",
            "first->band != next->band",
            "next->segment_num != first->segment_num + 1",
        ):
            if identity_guard not in mcu_c:
                raise AssertionError(
                    f"segment identity guard is missing: {identity_guard}"
                )

        dump = vendor_c.split("mt7915_vendor_csi_ctrl_dump", 1)[1]
        dequeue_unlock = dump.find("spin_unlock_bh(&phy->csi.csi_lock)")
        encode = dump.find("mt7915_csi_put_record")
        if dequeue_unlock < 0 or encode < 0 or dequeue_unlock > encode:
            raise AssertionError("netlink encoding still occurs under queue lock")

    print(f"patch applies cleanly to {EXPECTED_COMMIT}")
    print("ABI 0..27 and hardening invariants verified")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the hardened MT7915 CSI patch and TLV model"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(os.environ["MT76_BASELINE"])
        if "MT76_BASELINE" in os.environ
        else None,
        help="clean mt76 checkout at the pinned commit (or MT76_BASELINE)",
    )
    parser.add_argument(
        "--model-only",
        action="store_true",
        help="run hardware-free TLV/state-machine tests without a checkout",
    )
    args = parser.parse_args()

    if args.baseline and not args.model_only:
        verify_patch(args.baseline)
    elif not args.model_only:
        print(
            "No mt76 baseline supplied; running model tests only. "
            "Use --baseline /path/to/mt76 for apply/ABI verification."
        )

    suite = unittest.TestSuite(
        [
            unittest.defaultTestLoader.loadTestsFromTestCase(TlvModelTests),
            unittest.defaultTestLoader.loadTestsFromTestCase(
                SegmentAssemblerTests
            ),
        ]
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
