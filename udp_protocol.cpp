#include "udp_protocol.h"

#include <limits>

namespace
{
void append_u8(std::vector<uint8_t> &output, uint8_t value)
{
    output.push_back(value);
}

void append_u16(std::vector<uint8_t> &output, uint16_t value)
{
    output.push_back(static_cast<uint8_t>(value >> 8));
    output.push_back(static_cast<uint8_t>(value));
}

void append_u32(std::vector<uint8_t> &output, uint32_t value)
{
    output.push_back(static_cast<uint8_t>(value >> 24));
    output.push_back(static_cast<uint8_t>(value >> 16));
    output.push_back(static_cast<uint8_t>(value >> 8));
    output.push_back(static_cast<uint8_t>(value));
}

void append_u64(std::vector<uint8_t> &output, uint64_t value)
{
    append_u32(output, static_cast<uint32_t>(value >> 32));
    append_u32(output, static_cast<uint32_t>(value));
}
} // namespace

std::vector<uint8_t> csi_udp::encode_v2(const CsiPacket &packet,
                                        uint32_t sequence)
{
    if (packet.samples.empty() || packet.samples.size() > MAX_COMPLEX_SAMPLES ||
        packet.samples.size() > std::numeric_limits<uint16_t>::max())
        return {};

    const size_t message_size =
        V2_HEADER_SIZE + packet.samples.size() * 2U * sizeof(int16_t);
    if (message_size > MAX_UDP_DATAGRAM_SIZE ||
        message_size > std::numeric_limits<uint32_t>::max())
        return {};

    std::vector<uint8_t> output;
    output.reserve(message_size);

    output.insert(output.end(), {'C', 'S', 'I', '2'});
    append_u8(output, PROTOCOL_VERSION);
    append_u8(output, packet.metadata_flags);
    append_u16(output, static_cast<uint16_t>(V2_HEADER_SIZE));
    append_u32(output, static_cast<uint32_t>(message_size));
    append_u32(output, sequence);
    append_u64(output, packet.host_timestamp_ns);
    append_u32(output, packet.driver_timestamp);
    append_u32(output, packet.ext_info);
    append_u32(output, packet.h_idx);
    append_u32(output, packet.chain_info);
    output.insert(output.end(), packet.transmitter_address.begin(),
                  packet.transmitter_address.end());
    append_u8(output, static_cast<uint8_t>(packet.rssi));
    append_u8(output, packet.snr);
    append_u8(output, packet.channel_bandwidth);
    append_u8(output, packet.data_bandwidth);
    append_u8(output, packet.primary_channel_index);
    append_u8(output, packet.band);
    append_u8(output, packet.rx_mode);
    append_u8(output, packet.rate_mcs);
    append_u16(output, packet.tx_index);
    append_u16(output, packet.rx_index);
    append_u16(output, static_cast<uint16_t>(packet.samples.size()));
    append_u16(output, packet.channel_frequency_mhz);
    append_u8(output, SAMPLE_FORMAT_SIGNED_IQ16);
    append_u8(output, packet.rate_nss);
    append_u16(output, packet.packet_sequence_number);
    append_u32(output, packet.segment_number);
    append_u32(output, packet.rate_kbps);
    append_u16(output, packet.presence_flags);
    append_u8(output, packet.remain_last);
    append_u8(output, packet.transport_stream);
    append_u8(output, packet.rate_guard_interval);
    append_u8(output, 0); // Reserved.

    if (output.size() != V2_HEADER_SIZE)
        return {};

    for (const CsiIqSample &sample : packet.samples)
    {
        append_u16(output, static_cast<uint16_t>(sample.i));
        append_u16(output, static_cast<uint16_t>(sample.q));
    }

    return output;
}
