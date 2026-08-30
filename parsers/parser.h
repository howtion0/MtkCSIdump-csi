#pragma once

#include <array>
#include <cstdint>
#include <vector>

struct CsiIqSample
{
    int16_t i = 0;
    int16_t q = 0;
};

struct CsiPacket
{
    uint64_t host_timestamp_ns = 0;
    uint32_t driver_timestamp = 0;
    uint32_t ext_info = 0;
    uint32_t h_idx = 0;
    uint32_t chain_info = 0;
    std::array<uint8_t, 6> transmitter_address{};
    int8_t rssi = 0;
    uint8_t snr = 0;
    uint8_t channel_bandwidth = 0;
    uint8_t data_bandwidth = 0;
    uint8_t primary_channel_index = 0;
    uint8_t band = 0;
    uint8_t rx_mode = 0;
    uint16_t tx_index = 0;
    uint16_t rx_index = 0;
    uint16_t channel_frequency_mhz = 0;
    uint8_t metadata_flags = 0;
    uint16_t presence_flags = 0;
    uint16_t packet_sequence_number = 0;
    uint32_t segment_number = 0;
    uint8_t remain_last = 0;
    uint8_t transport_stream = 0;
    uint8_t rate_mcs = 0xff;
    uint8_t rate_nss = 0;
    uint8_t rate_guard_interval = 0xff;
    uint32_t rate_kbps = 0;
    std::vector<CsiIqSample> samples;
};

class Parser
{
public:
    virtual ~Parser() = default;
    virtual std::vector<CsiPacket> processRawData(void *data) = 0;
};
