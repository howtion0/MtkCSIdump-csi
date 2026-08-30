// Generates stage1_encoder_v2.csi2 with the actual Stage-1 encoder API.
// Provenance is recorded in stage1_encoder_v2.provenance.json.
#include "udp_protocol.h"

#include <cstdio>

int main()
{
    CsiPacket packet;
    packet.host_timestamp_ns = 0x0102030405060708ULL;
    packet.driver_timestamp = 0x11223344;
    packet.ext_info = 0x55667788;
    packet.h_idx = 0x99aabbcc;
    packet.chain_info = 0xddeeff00;
    packet.transmitter_address = {0, 1, 2, 3, 4, 5};
    packet.rssi = -42;
    packet.snr = 33;
    packet.channel_bandwidth = 0;
    packet.data_bandwidth = 0;
    packet.primary_channel_index = 0;
    packet.band = 1;
    packet.rx_mode = 8;
    packet.rate_mcs = 7;
    packet.rate_nss = 2;
    packet.rate_guard_interval = 1;
    packet.tx_index = 0x1234;
    packet.rx_index = 0x5678;
    packet.channel_frequency_mhz = 5180;
    packet.metadata_flags = 1U << 5; // TONE_MASKED_REORDERED
    packet.presence_flags = 0x0fff;
    packet.packet_sequence_number = 0xabcd;
    packet.segment_number = 0x10203040;
    packet.remain_last = 0;
    packet.transport_stream = 3;
    packet.rate_kbps = 300000;
    for (int index = 0; index < 64; ++index)
        packet.samples.push_back({static_cast<int16_t>(-100 + index),
                                  static_cast<int16_t>(200 - 2 * index)});
    const auto encoded = csi_udp::encode_v2(packet, 0x0a0b0c0d);
    return std::fwrite(encoded.data(), 1, encoded.size(), stdout) == encoded.size()
               ? 0
               : 1;
}
