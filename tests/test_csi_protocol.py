import struct
import unittest

import csi_protocol


class ProtocolTests(unittest.TestCase):
    def test_v2_header_layout_and_metadata(self):
        self.assertEqual(csi_protocol.V2_HEADER_SIZE, 80)
        samples = struct.pack(">hhhh", -1, 2, 32767, -32768)
        message_size = csi_protocol.V2_HEADER_SIZE + len(samples)
        header = struct.pack(
            csi_protocol.V2_HEADER_FORMAT,
            b"CSI2",
            2,
            0x05,
            csi_protocol.V2_HEADER_SIZE,
            message_size,
            17,
            1_700_000_000_123_456_789,
            0x11223344,
            0x55667788,
            0x99AABBCC,
            0xDDEEFF00,
            bytes.fromhex("001122334455"),
            -47,
            31,
            2,
            2,
            1,
            1,
            8,
            0xFF,
            0x1234,
            0x5678,
            2,
            5180,
            csi_protocol.V2_SAMPLE_FORMAT_SIGNED_IQ16,
            0,
            0xABCD,
            0x10203040,
            0,
            0x007C,
            1,
            3,
            0xFF,
            0,
        )

        decoded = csi_protocol.decode_datagram(header + samples, ("router", 8888))
        self.assertEqual(decoded.protocol_version, 2)
        self.assertEqual(decoded.flags, 0x05)
        self.assertEqual(decoded.packet_count, 17)
        self.assertEqual(decoded.driver_timestamp, 0x11223344)
        self.assertEqual(decoded.transmitter_address, bytes.fromhex("001122334455"))
        self.assertEqual(decoded.rssi, -47)
        self.assertEqual(decoded.antenna_idx, 0x5678)
        self.assertEqual(decoded.tx_idx, 0x1234)
        self.assertEqual(decoded.channel_frequency_mhz, 5180)
        self.assertEqual(decoded.packet_sequence_number, 0xABCD)
        self.assertEqual(decoded.segment_number, 0x10203040)
        self.assertEqual(decoded.remain_last, 1)
        self.assertEqual(decoded.transport_stream, 3)
        self.assertEqual(decoded.samples, [complex(-1, 2), complex(32767, -32768)])

    def test_legacy_v1_remains_readable(self):
        header = struct.pack(csi_protocol.V1_HEADER_FORMAT, 1234, 2, 9, 2)
        samples = struct.pack("<dddd", 1.5, -2.5, 3.0, 4.0)
        decoded = csi_protocol.decode_datagram(header + samples)
        self.assertEqual(decoded.protocol_version, 1)
        self.assertEqual(decoded.timestamp, 1234)
        self.assertEqual(decoded.antenna_idx, 2)
        self.assertEqual(decoded.packet_count, 9)
        self.assertEqual(decoded.samples, [complex(1.5, -2.5), complex(3.0, 4.0)])

    def test_truncated_v2_is_rejected(self):
        with self.assertRaises(csi_protocol.ProtocolError):
            csi_protocol.decode_datagram(b"CSI2\x02")

    def test_v1_declared_length_must_match(self):
        header = struct.pack(csi_protocol.V1_HEADER_FORMAT, 0, 0, 1, 2)
        with self.assertRaises(csi_protocol.ProtocolError):
            csi_protocol.decode_datagram(header + struct.pack("<dd", 1.0, 2.0))

    def test_v1_oversize_sample_count_is_rejected(self):
        header = struct.pack(
            csi_protocol.V1_HEADER_FORMAT,
            0,
            0,
            1,
            csi_protocol.MAX_COMPLEX_SAMPLES + 1,
        )
        with self.assertRaises(csi_protocol.ProtocolError):
            csi_protocol.decode_datagram(header)

    def test_v2_oversize_data_num_is_rejected(self):
        header = struct.pack(
            csi_protocol.V2_HEADER_FORMAT,
            b"CSI2",
            2,
            0,
            csi_protocol.V2_HEADER_SIZE,
            csi_protocol.V2_HEADER_SIZE,
            0,
            0,
            0,
            0,
            0,
            0,
            b"\0" * 6,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0xFF,
            0,
            0,
            csi_protocol.MAX_COMPLEX_SAMPLES + 1,
            0,
            csi_protocol.V2_SAMPLE_FORMAT_SIGNED_IQ16,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0xFF,
            0,
        )
        with self.assertRaises(csi_protocol.ProtocolError):
            csi_protocol.decode_datagram(header)


if __name__ == "__main__":
    unittest.main()
