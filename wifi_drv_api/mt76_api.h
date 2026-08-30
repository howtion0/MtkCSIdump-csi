#ifndef __MT76_VENDOR_H
#define __MT76_VENDOR_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int8_t s8;
typedef int16_t s16;
typedef int32_t s32;
typedef int64_t s64, ktime_t;

#define CSI_BW20_DATA_COUNT 64
#define CSI_BW40_DATA_COUNT 128
#define CSI_BW80_DATA_COUNT 256
#define CSI_BW160_DATA_COUNT 512
#define CSI_BW320_DATA_COUNT 1024
#define ETH_ALEN 6

enum CsiMetadataFlags : u8
{
    CSI_META_CH_BW_INFERRED = 1U << 0,
    CSI_META_DATA_NUM_INFERRED = 1U << 1,
    CSI_META_EXTENDED_ABI = 1U << 2,
    CSI_META_TRUNCATED = 1U << 3,
    CSI_META_FREQ_IS_PRIMARY = 1U << 4,
    CSI_META_TONE_MASKED_REORDERED = 1U << 5,
};

enum CsiPresenceFlags : u16
{
    CSI_PRESENT_H_IDX = 1U << 0,
    CSI_PRESENT_CHAIN_INFO = 1U << 1,
    CSI_PRESENT_PKT_SN = 1U << 2,
    CSI_PRESENT_SEGMENT_NUM = 1U << 3,
    CSI_PRESENT_REMAIN_LAST = 1U << 4,
    CSI_PRESENT_TR_STREAM = 1U << 5,
    CSI_PRESENT_RX_MODE = 1U << 6,
    CSI_PRESENT_RATE_MCS = 1U << 7,
    CSI_PRESENT_RATE_NSS = 1U << 8,
    CSI_PRESENT_RATE_KBPS = 1U << 9,
    CSI_PRESENT_CHANNEL_FREQ = 1U << 10,
    CSI_PRESENT_BAND = 1U << 11,
};

struct csi_data
{
    u8 version = 0;
    u8 ch_bw = 0;
    u16 data_num = 0;
    std::array<s16, CSI_BW320_DATA_COUNT> data_i{};
    std::array<s16, CSI_BW320_DATA_COUNT> data_q{};
    u8 band = 0;
    s8 rssi = 0;
    u8 snr = 0;
    u64 host_timestamp_ns = 0;
    u32 ts = 0;
    u8 data_bw = 0;
    u8 pri_ch_idx = 0;
    u16 channel_frequency_mhz = 0;
    std::array<u8, ETH_ALEN> ta{};
    u32 ext_info = 0;
    u8 rx_mode = 0;
    u32 chain_info = 0;
    u16 tx_idx = 0;
    u16 rx_idx = 0;
    u32 segment_num = 0;
    u8 remain_last = 0;
    u16 pkt_sn = 0;
    u8 tr_stream = 0;
    u32 h_idx = 0;
    u8 metadata_flags = 0;
    u16 presence_flags = 0;
};

size_t csi_fft_count_for_bandwidth(u8 bandwidth);
size_t csi_valid_sample_count(const csi_data &data);
bool csi_validate_dense_indices(const std::vector<size_t> &indices,
                                size_t capacity, size_t &dense_count);

class MT76APIPrivate;
class MT76API
{
public:
    MT76API();
    ~MT76API();

    int motion_detection_start(const char *wifi);
    int motion_detection_stop(const char *wifi);
    std::vector<csi_data *> *motion_detection_dump(const char *wifi, int pkt_num);

private:
    MT76APIPrivate *d;
};

#endif
