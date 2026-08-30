#include "parsers/parser_mt76.h"
#include "udp_protocol.h"
#include "wifi_drv_api/mt76_api.h"

#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

namespace
{
void expect(bool condition, const std::string &message)
{
    if (!condition)
    {
        std::cerr << "FAILED: " << message << '\n';
        std::exit(EXIT_FAILURE);
    }
}

uint16_t read_u16(const std::vector<uint8_t> &data, size_t offset)
{
    return static_cast<uint16_t>((static_cast<uint16_t>(data.at(offset)) << 8) |
                                 data.at(offset + 1));
}

uint32_t read_u32(const std::vector<uint8_t> &data, size_t offset)
{
    return (static_cast<uint32_t>(data.at(offset)) << 24) |
           (static_cast<uint32_t>(data.at(offset + 1)) << 16) |
           (static_cast<uint32_t>(data.at(offset + 2)) << 8) |
           data.at(offset + 3);
}

void test_bandwidth_and_dynamic_chains()
{
    csi_data eighty_mhz;
    eighty_mhz.data_bw = 2;
    eighty_mhz.ch_bw = 2;
    eighty_mhz.rx_idx = 2;
    eighty_mhz.tx_idx = 1;
    eighty_mhz.rssi = -47;
    eighty_mhz.snr = 31;
    eighty_mhz.channel_frequency_mhz = 5210;
    eighty_mhz.presence_flags = CSI_PRESENT_CHANNEL_FREQ;
    eighty_mhz.metadata_flags =
        CSI_META_CH_BW_INFERRED | CSI_META_DATA_NUM_INFERRED |
        CSI_META_TONE_MASKED_REORDERED;
    for (size_t index = 0; index < CSI_BW80_DATA_COUNT; ++index)
    {
        eighty_mhz.data_i[index] = static_cast<int16_t>(index);
        eighty_mhz.data_q[index] = -static_cast<int16_t>(index);
    }

    csi_data forty_mhz;
    forty_mhz.data_bw = 1;
    forty_mhz.ch_bw = 1;
    forty_mhz.data_num = CSI_BW40_DATA_COUNT;
    forty_mhz.rx_idx = 7; // Proves the parser has no three-antenna ceiling.

    std::vector<csi_data *> raw{&eighty_mhz, &forty_mhz};
    ParserMT76 parser;
    const std::vector<CsiPacket> packets = parser.processRawData(&raw);

    expect(packets.size() == 2, "both receive chains are retained");
    expect(packets[0].rx_index == 2, "first rx_idx is preserved");
    expect(packets[1].rx_index == 7, "arbitrary rx_idx is preserved");
    expect(packets[0].samples.size() == CSI_BW80_DATA_COUNT,
           "80 MHz capture contains 256 points, not the old 61");
    expect(packets[1].samples.size() == CSI_BW40_DATA_COUNT,
           "explicit 40 MHz data_num is honored");
    expect(packets[0].samples[255].i == 255 &&
               packets[0].samples[255].q == -255,
           "last valid I/Q pair survives parsing");
    expect(packets[0].channel_frequency_mhz == 5210 &&
               (packets[0].presence_flags & CSI_PRESENT_CHANNEL_FREQ),
           "queried channel frequency and its presence bit survive parsing");
    expect(packets[0].metadata_flags & CSI_META_TONE_MASKED_REORDERED,
           "audited driver tone-order state survives parsing");
}

void test_nested_attribute_indices()
{
    size_t count = 0;
    expect(csi_validate_dense_indices({2, 0, 1}, 8, count) && count == 3,
           "out-of-order nested attributes are reordered by their NLA index");
    expect(!csi_validate_dense_indices({0, 1, 1}, 8, count),
           "duplicate nested attributes are rejected");
    expect(!csi_validate_dense_indices({0, 2}, 8, count),
           "gapped nested attributes are rejected");
    expect(!csi_validate_dense_indices({0, 8}, 8, count),
           "out-of-range nested attributes are rejected");
}

void test_udp_v2_network_order()
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
    packet.channel_bandwidth = 2;
    packet.data_bandwidth = 2;
    packet.primary_channel_index = 1;
    packet.band = 1;
    packet.rx_mode = 8;
    packet.tx_index = 0x1234;
    packet.rx_index = 0x5678;
    packet.channel_frequency_mhz = 5180;
    packet.metadata_flags = CSI_META_EXTENDED_ABI;
    packet.presence_flags = CSI_PRESENT_CHAIN_INFO | CSI_PRESENT_PKT_SN |
                            CSI_PRESENT_SEGMENT_NUM | CSI_PRESENT_REMAIN_LAST |
                            CSI_PRESENT_TR_STREAM | CSI_PRESENT_RX_MODE;
    packet.packet_sequence_number = 0xabcd;
    packet.segment_number = 0x10203040;
    packet.remain_last = 1;
    packet.transport_stream = 3;
    packet.samples = {{-1, 2}, {32767, -32768}};

    const std::vector<uint8_t> encoded =
        csi_udp::encode_v2(packet, 0x0a0b0c0d);
    expect(encoded.size() == csi_udp::V2_HEADER_SIZE + 8,
           "v2 message has an exact fixed header and IQ16 payload");
    expect(std::string(encoded.begin(), encoded.begin() + 4) == "CSI2",
           "v2 magic is present");
    expect(encoded[4] == 2, "v2 version byte is present");
    expect(read_u16(encoded, 6) == csi_udp::V2_HEADER_SIZE,
           "header length is network ordered");
    expect(read_u32(encoded, 8) == encoded.size(),
           "message length is network ordered");
    expect(read_u32(encoded, 12) == 0x0a0b0c0d,
           "sequence is network ordered");
    expect(encoded[46] == static_cast<uint8_t>(-42),
           "signed RSSI keeps its bit representation");
    expect(read_u16(encoded, 54) == 0x1234 &&
               read_u16(encoded, 56) == 0x5678,
           "TX/RX indexes are 16-bit values");
    expect(read_u16(encoded, 58) == 2, "data_num matches sample count");
    expect(read_u16(encoded, 60) == 5180, "channel frequency is exported");
    expect(encoded[62] == csi_udp::SAMPLE_FORMAT_SIGNED_IQ16,
           "sample format is explicit");
    expect(read_u16(encoded, 64) == 0xabcd,
           "firmware packet sequence number is preserved");
    expect(read_u32(encoded, 66) == 0x10203040,
           "firmware segment number is preserved");
    expect(read_u16(encoded, 74) == packet.presence_flags,
           "optional metadata has an explicit presence bitmap");
    expect(encoded[76] == 1 && encoded[77] == 3,
           "segment completion and transport stream are preserved");
    expect(encoded[80] == 0xff && encoded[81] == 0xff &&
               encoded[82] == 0x00 && encoded[83] == 0x02 &&
               encoded[84] == 0x7f && encoded[85] == 0xff &&
               encoded[86] == 0x80 && encoded[87] == 0x00,
           "signed I/Q samples use network byte order");

    packet.samples.resize(csi_udp::MAX_COMPLEX_SAMPLES + 1);
    expect(csi_udp::encode_v2(packet, 1).empty(),
           "oversize CSI vectors are rejected before sendto");
}
} // namespace

int main()
{
    test_bandwidth_and_dynamic_chains();
    test_nested_attribute_indices();
    test_udp_v2_network_order();
    std::cout << "All capture tests passed\n";
    return EXIT_SUCCESS;
}
