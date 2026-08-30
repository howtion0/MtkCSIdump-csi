#include "parser_mt76.h"

#include "wifi_drv_api/mt76_api.h"

#include <chrono>
#include <utility>

std::vector<CsiPacket> ParserMT76::processRawData(void *data)
{
    std::vector<CsiPacket> packets;
    if (!data)
        return packets;

    const auto *list = static_cast<const std::vector<csi_data *> *>(data);
    packets.reserve(list->size());

    for (const csi_data *raw : *list)
    {
        if (!raw)
            continue;

        const size_t sample_count = csi_valid_sample_count(*raw);
        if (sample_count == 0)
            continue;

        CsiPacket packet;
        packet.host_timestamp_ns = raw->host_timestamp_ns;
        if (!packet.host_timestamp_ns)
        {
            packet.host_timestamp_ns = static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::system_clock::now().time_since_epoch())
                    .count());
        }
        packet.driver_timestamp = raw->ts;
        packet.ext_info = raw->ext_info;
        packet.h_idx = raw->h_idx;
        packet.chain_info = raw->chain_info;
        packet.transmitter_address = raw->ta;
        packet.rssi = raw->rssi;
        packet.snr = raw->snr;
        packet.channel_bandwidth = raw->ch_bw;
        packet.data_bandwidth = raw->data_bw;
        packet.primary_channel_index = raw->pri_ch_idx;
        packet.band = raw->band;
        packet.rx_mode = raw->rx_mode;
        packet.tx_index = raw->tx_idx;
        packet.rx_index = raw->rx_idx;
        packet.metadata_flags = raw->metadata_flags;
        packet.presence_flags = raw->presence_flags;
        packet.packet_sequence_number = raw->pkt_sn;
        packet.segment_number = raw->segment_num;
        packet.remain_last = raw->remain_last;
        packet.transport_stream = raw->tr_stream;
        packet.samples.reserve(sample_count);

        // Keep the exact carrier order and all valid bins. Pilot/DC masking is
        // a signal-processing decision and must not destroy capture data.
        for (size_t index = 0; index < sample_count; ++index)
            packet.samples.push_back({raw->data_i[index], raw->data_q[index]});

        packets.push_back(std::move(packet));
    }

    return packets;
}
