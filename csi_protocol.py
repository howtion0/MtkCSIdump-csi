"""Wire decoders shared by the GUI and hardware-free protocol tests."""

import struct


V1_HEADER_FORMAT = "<QIII"
V1_HEADER_SIZE = struct.calcsize(V1_HEADER_FORMAT)
V1_SAMPLE_FORMAT = "<dd"
V1_SAMPLE_SIZE = struct.calcsize(V1_SAMPLE_FORMAT)

V2_MAGIC = b"CSI2"
V2_HEADER_FORMAT = ">4sBBHIIQIIII6sbBBBBBBBHHHHBBHIIHBBBB"
V2_HEADER_SIZE = struct.calcsize(V2_HEADER_FORMAT)
V2_SAMPLE_FORMAT = ">hh"
V2_SAMPLE_SIZE = struct.calcsize(V2_SAMPLE_FORMAT)
V2_SAMPLE_FORMAT_SIGNED_IQ16 = 1
MAX_COMPLEX_SAMPLES = 1024
MAX_UDP_DATAGRAM_SIZE = 65507

CSI_PRESENT_H_IDX = 1 << 0
CSI_PRESENT_CHAIN_INFO = 1 << 1
CSI_PRESENT_PKT_SN = 1 << 2
CSI_PRESENT_SEGMENT_NUM = 1 << 3
CSI_PRESENT_REMAIN_LAST = 1 << 4
CSI_PRESENT_TR_STREAM = 1 << 5
CSI_PRESENT_RX_MODE = 1 << 6
CSI_PRESENT_RATE_MCS = 1 << 7
CSI_PRESENT_RATE_NSS = 1 << 8
CSI_PRESENT_RATE_KBPS = 1 << 9
CSI_PRESENT_CHANNEL_FREQ = 1 << 10
CSI_PRESENT_BAND = 1 << 11


class ProtocolError(ValueError):
    """Raised when a UDP datagram is not a complete CSI packet."""


class CSIData:
    def __init__(self):
        self.protocol_version = 0
        self.flags = 0
        self.timestamp = 0
        self.host_timestamp_ns = 0
        self.driver_timestamp = 0
        self.antenna_idx = 0
        self.tx_idx = 0
        self.packet_count = 0
        self.samples = []
        self.addr = None
        self.transmitter_address = b"\x00" * 6
        self.rssi = 0
        self.snr = 0
        self.channel_bandwidth = 0
        self.data_bandwidth = 0
        self.primary_channel_index = 0
        self.band = 0
        self.rx_mode = 0
        self.ext_info = 0
        self.h_idx = 0
        self.chain_info = 0
        self.channel_frequency_mhz = 0
        self.presence_flags = 0
        self.packet_sequence_number = 0
        self.segment_number = 0
        self.remain_last = 0
        self.transport_stream = 0
        self.rate_mcs = 0xff
        self.rate_nss = 0
        self.rate_guard_interval = 0xff
        self.rate_kbps = 0


def _decode_v1(data, addr):
    if len(data) < V1_HEADER_SIZE:
        raise ProtocolError("v1 datagram is shorter than its header")

    timestamp, antenna_idx, packet_count, total_samples = struct.unpack_from(
        V1_HEADER_FORMAT, data
    )
    if total_samples == 0 or total_samples > MAX_COMPLEX_SAMPLES:
        raise ProtocolError("v1 sample count is outside the supported CSI range")
    expected_size = V1_HEADER_SIZE + total_samples * V1_SAMPLE_SIZE
    if expected_size != len(data):
        raise ProtocolError(
            "v1 length mismatch: header declares {} bytes, received {}".format(
                expected_size, len(data)
            )
        )

    result = CSIData()
    result.protocol_version = 1
    result.timestamp = timestamp
    result.host_timestamp_ns = timestamp * 1_000_000
    result.antenna_idx = antenna_idx
    result.packet_count = packet_count
    result.addr = addr
    result.samples = [
        complex(i_value, q_value)
        for i_value, q_value in struct.iter_unpack(
            V1_SAMPLE_FORMAT, data[V1_HEADER_SIZE:]
        )
    ]
    return result


def _decode_v2(data, addr):
    if len(data) < V2_HEADER_SIZE:
        raise ProtocolError("v2 datagram is shorter than its header")

    (
        magic,
        version,
        flags,
        header_size,
        message_size,
        sequence,
        host_timestamp_ns,
        driver_timestamp,
        ext_info,
        h_idx,
        chain_info,
        transmitter_address,
        rssi,
        snr,
        channel_bandwidth,
        data_bandwidth,
        primary_channel_index,
        band,
        rx_mode,
        rate_mcs,
        tx_idx,
        rx_idx,
        data_num,
        channel_frequency_mhz,
        sample_format,
        rate_nss,
        packet_sequence_number,
        segment_number,
        rate_kbps,
        presence_flags,
        remain_last,
        transport_stream,
        rate_guard_interval,
        _reserved,
    ) = struct.unpack_from(V2_HEADER_FORMAT, data)

    if magic != V2_MAGIC or version != 2:
        raise ProtocolError("unsupported CSI v2 magic/version")
    if header_size != V2_HEADER_SIZE:
        raise ProtocolError("unsupported CSI v2 header size")
    if message_size != len(data):
        raise ProtocolError(
            "v2 length mismatch: header declares {} bytes, received {}".format(
                message_size, len(data)
            )
        )
    if sample_format != V2_SAMPLE_FORMAT_SIGNED_IQ16:
        raise ProtocolError("unsupported CSI v2 sample format")
    if data_num == 0 or data_num > MAX_COMPLEX_SAMPLES:
        raise ProtocolError("v2 data_num is outside the supported CSI range")
    if header_size + data_num * V2_SAMPLE_SIZE != message_size:
        raise ProtocolError("v2 data_num does not match payload length")

    result = CSIData()
    result.protocol_version = 2
    result.flags = flags
    result.timestamp = host_timestamp_ns // 1_000_000
    result.host_timestamp_ns = host_timestamp_ns
    result.driver_timestamp = driver_timestamp
    result.antenna_idx = rx_idx
    result.tx_idx = tx_idx
    result.packet_count = sequence
    result.addr = addr
    result.transmitter_address = transmitter_address
    result.rssi = rssi
    result.snr = snr
    result.channel_bandwidth = channel_bandwidth
    result.data_bandwidth = data_bandwidth
    result.primary_channel_index = primary_channel_index
    result.band = band
    result.rx_mode = rx_mode
    result.ext_info = ext_info
    result.h_idx = h_idx
    result.chain_info = chain_info
    result.channel_frequency_mhz = channel_frequency_mhz
    result.presence_flags = presence_flags
    result.packet_sequence_number = packet_sequence_number
    result.segment_number = segment_number
    result.remain_last = remain_last
    result.transport_stream = transport_stream
    result.rate_mcs = rate_mcs
    result.rate_nss = rate_nss
    result.rate_guard_interval = rate_guard_interval
    result.rate_kbps = rate_kbps
    result.samples = [
        complex(i_value, q_value)
        for i_value, q_value in struct.iter_unpack(
            V2_SAMPLE_FORMAT, data[header_size:]
        )
    ]
    return result


def decode_datagram(data, addr=None):
    """Decode protocol v2, falling back to the repository's legacy v1."""

    if len(data) > MAX_UDP_DATAGRAM_SIZE:
        raise ProtocolError("datagram exceeds the IPv4 UDP payload limit")
    if data.startswith(V2_MAGIC):
        return _decode_v2(data, addr)
    return _decode_v1(data, addr)
