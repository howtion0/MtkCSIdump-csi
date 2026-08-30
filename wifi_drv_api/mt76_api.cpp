#include "mt76_api.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <new>
#include <vector>

#include <linux/nl80211.h>

extern "C"
{
#include <netlink/attr.h>
#include <netlink/msg.h>
#include <unl.h>
}

#include <net/if.h>

namespace
{
constexpr u32 MTK_NL80211_VENDOR_ID = 0x0ce7;
constexpr int CSI_DUMP_PER_REQUEST = 3;
constexpr int CSI_MAX_DUMP_PACKETS = 30000;

enum MtkNl80211VendorSubcmds
{
    MTK_NL80211_VENDOR_SUBCMD_CSI_CTRL = 0xc2,
};

enum MtkVendorAttrCsiCtrl
{
    MTK_VENDOR_ATTR_CSI_CTRL_UNSPEC,
    MTK_VENDOR_ATTR_CSI_CTRL_CFG,
    MTK_VENDOR_ATTR_CSI_CTRL_CFG_MODE,
    MTK_VENDOR_ATTR_CSI_CTRL_CFG_TYPE,
    MTK_VENDOR_ATTR_CSI_CTRL_CFG_VAL1,
    MTK_VENDOR_ATTR_CSI_CTRL_CFG_VAL2,
    MTK_VENDOR_ATTR_CSI_CTRL_MAC_ADDR,
    MTK_VENDOR_ATTR_CSI_CTRL_INTERVAL,
    MTK_VENDOR_ATTR_CSI_CTRL_DUMP_NUM,
    MTK_VENDOR_ATTR_CSI_CTRL_DATA,
    NUM_MTK_VENDOR_ATTRS_CSI_CTRL,
    MTK_VENDOR_ATTR_CSI_CTRL_MAX = NUM_MTK_VENDOR_ATTRS_CSI_CTRL - 1,
};

// Attributes 2..8 are shared by the Nullcon ABI and MediaTek's extended ABI.
// Starting at attribute 9, the extended ABI inserts DATA_NUM and shifts the
// remaining fields by one.
enum MtkVendorAttrCsiDataCommon
{
    MTK_VENDOR_ATTR_CSI_DATA_UNSPEC,
    MTK_VENDOR_ATTR_CSI_DATA_PAD,
    MTK_VENDOR_ATTR_CSI_DATA_VER,
    MTK_VENDOR_ATTR_CSI_DATA_TS,
    MTK_VENDOR_ATTR_CSI_DATA_RSSI,
    MTK_VENDOR_ATTR_CSI_DATA_SNR,
    MTK_VENDOR_ATTR_CSI_DATA_BW,
    MTK_VENDOR_ATTR_CSI_DATA_CH_IDX,
    MTK_VENDOR_ATTR_CSI_DATA_TA,
};

constexpr int SIMPLE_DATA_I = 9;
constexpr int SIMPLE_DATA_Q = 10;
constexpr int SIMPLE_DATA_INFO = 11;
constexpr int SIMPLE_DATA_TX_ANT = 16;
constexpr int SIMPLE_DATA_RX_ANT = 17;
constexpr int SIMPLE_DATA_MODE = 18;
constexpr int SIMPLE_DATA_H_IDX = 19;

constexpr int EXTENDED_DATA_NUM = 9;
constexpr int EXTENDED_DATA_I = 10;
constexpr int EXTENDED_DATA_Q = 11;
constexpr int EXTENDED_DATA_INFO = 12;
constexpr int EXTENDED_DATA_TX_ANT = 17;
constexpr int EXTENDED_DATA_RX_ANT = 18;
constexpr int EXTENDED_DATA_MODE = 19;
constexpr int EXTENDED_DATA_CHAIN_INFO = 20;

// Stage-2 ABI additions preserve every Nullcon ID and append new metadata.
constexpr int STAGE2_DATA_CH_BW = 20;
constexpr int STAGE2_DATA_NUM = 21;
constexpr int STAGE2_DATA_PKT_SN = 22;
constexpr int STAGE2_DATA_SEGMENT_NUM = 23;
constexpr int STAGE2_DATA_REMAIN_LAST = 24;
constexpr int STAGE2_DATA_TR_STREAM = 25;
constexpr int STAGE2_DATA_CHAIN_INFO = 26;
constexpr int STAGE2_DATA_BAND = 27;
constexpr int MAX_CSI_DATA_ATTR = STAGE2_DATA_BAND;

struct DumpContext
{
    struct unl *netlink;
    std::vector<csi_data *> *output;
    u16 channel_frequency_mhz;
    u8 channel_bandwidth;
    bool frequency_is_primary;
    bool tone_masked_reordered;
    bool parse_failed = false;
};

void clear_csi_list(std::vector<csi_data *> &list)
{
    for (csi_data *entry : list)
        delete entry;
    list.clear();
}

bool attr_u8(struct nlattr *attr, u8 &value)
{
    if (!attr || nla_len(attr) != static_cast<int>(sizeof(u8)))
        return false;
    value = nla_get_u8(attr);
    return true;
}

bool attr_u16(struct nlattr *attr, u16 &value)
{
    if (!attr || nla_len(attr) != static_cast<int>(sizeof(u16)))
        return false;
    value = nla_get_u16(attr);
    return true;
}

bool attr_u32(struct nlattr *attr, u32 &value)
{
    if (!attr || nla_len(attr) != static_cast<int>(sizeof(u32)))
        return false;
    value = nla_get_u32(attr);
    return true;
}

int read_interface_channel(unsigned if_index, u16 &frequency_mhz,
                           u8 &channel_bandwidth,
                           bool &frequency_is_primary)
{
    frequency_mhz = 0;
    channel_bandwidth = 0;
    frequency_is_primary = false;

    struct unl netlink = {};
    if (unl_genl_init(&netlink, "nl80211") < 0)
        return -ENOLINK;

    struct nl_msg *request =
        unl_genl_msg(&netlink, NL80211_CMD_GET_INTERFACE, false);
    if (!request)
    {
        unl_free(&netlink);
        return -ENOMEM;
    }
    if (nla_put_u32(request, NL80211_ATTR_IFINDEX, if_index))
    {
        nlmsg_free(request);
        unl_free(&netlink);
        return -EMSGSIZE;
    }

    struct nl_msg *reply = nullptr;
    const int ret = unl_genl_request_single(&netlink, request, &reply);
    if (ret < 0 || !reply)
    {
        if (reply)
            nlmsg_free(reply);
        unl_free(&netlink);
        return ret < 0 ? ret : -ENODATA;
    }

    u32 raw_frequency = 0;
    struct nlattr *frequency =
        unl_find_attr(&netlink, reply, NL80211_ATTR_CENTER_FREQ1);
    if (!attr_u32(frequency, raw_frequency))
    {
        frequency = unl_find_attr(&netlink, reply, NL80211_ATTR_WIPHY_FREQ);
        if (!attr_u32(frequency, raw_frequency))
        {
            nlmsg_free(reply);
            unl_free(&netlink);
            return -ENODATA;
        }
        frequency_is_primary = true;
    }

    u32 raw_width = 0;
    struct nlattr *width =
        unl_find_attr(&netlink, reply, NL80211_ATTR_CHANNEL_WIDTH);
    if (!attr_u32(width, raw_width))
    {
        nlmsg_free(reply);
        unl_free(&netlink);
        return -ENODATA;
    }
    switch (raw_width)
    {
    case NL80211_CHAN_WIDTH_20_NOHT:
    case NL80211_CHAN_WIDTH_20:
        channel_bandwidth = 0;
        break;
    case NL80211_CHAN_WIDTH_40:
        channel_bandwidth = 1;
        break;
    case NL80211_CHAN_WIDTH_80:
        channel_bandwidth = 2;
        break;
    default:
        nlmsg_free(reply);
        unl_free(&netlink);
        return -EOPNOTSUPP;
    }

    nlmsg_free(reply);
    unl_free(&netlink);
    if (raw_frequency == 0 || raw_frequency > UINT16_MAX)
        return -ERANGE;
    frequency_mhz = static_cast<u16>(raw_frequency);
    return 0;
}

template <size_t N>
bool read_nested_u8(struct nlattr *attr, std::array<u8, N> &values)
{
    if (!attr)
        return false;

    struct nlattr *current;
    int remaining;
    std::vector<size_t> indices;
    nla_for_each_nested(current, attr, remaining)
    {
        const size_t index = static_cast<size_t>(nla_type(current));
        if (nla_len(current) != static_cast<int>(sizeof(u8)) || index >= N)
            return false;
        values[index] = nla_get_u8(current);
        indices.push_back(index);
    }
    size_t dense_count = 0;
    return csi_validate_dense_indices(indices, N, dense_count) &&
           dense_count == N;
}

template <size_t N>
bool read_nested_s16(struct nlattr *attr, std::array<s16, N> &values,
                     size_t &count)
{
    if (!attr)
        return false;

    struct nlattr *current;
    int remaining;
    std::vector<size_t> indices;
    nla_for_each_nested(current, attr, remaining)
    {
        const size_t index = static_cast<size_t>(nla_type(current));
        if (nla_len(current) != static_cast<int>(sizeof(u16)) || index >= N)
            return false;
        values[index] = static_cast<s16>(nla_get_u16(current));
        indices.push_back(index);
    }

    return csi_validate_dense_indices(indices, N, count);
}

bool parse_csi_data(struct nlattr *nested, csi_data &csi)
{
    struct nlattr *attrs[MAX_CSI_DATA_ATTR + 1] = {};
    if (nla_parse_nested(attrs, MAX_CSI_DATA_ATTR, nested, nullptr) < 0)
        return false;

    u8 raw_rssi = 0;
    if (!attr_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_VER], csi.version) ||
        !attr_u32(attrs[MTK_VENDOR_ATTR_CSI_DATA_TS], csi.ts) ||
        !attr_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_RSSI], raw_rssi) ||
        !attr_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_SNR], csi.snr) ||
        !attr_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_BW], csi.data_bw) ||
        !attr_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_CH_IDX], csi.pri_ch_idx) ||
        !read_nested_u8(attrs[MTK_VENDOR_ATTR_CSI_DATA_TA], csi.ta))
        return false;

    csi.rssi = static_cast<s8>(raw_rssi);

    const bool extended_abi =
        attrs[EXTENDED_DATA_NUM] &&
        nla_len(attrs[EXTENDED_DATA_NUM]) == static_cast<int>(sizeof(u32));

    const int i_attr = extended_abi ? EXTENDED_DATA_I : SIMPLE_DATA_I;
    const int q_attr = extended_abi ? EXTENDED_DATA_Q : SIMPLE_DATA_Q;
    const int info_attr = extended_abi ? EXTENDED_DATA_INFO : SIMPLE_DATA_INFO;
    const int tx_attr = extended_abi ? EXTENDED_DATA_TX_ANT : SIMPLE_DATA_TX_ANT;
    const int rx_attr = extended_abi ? EXTENDED_DATA_RX_ANT : SIMPLE_DATA_RX_ANT;
    const int mode_attr = extended_abi ? EXTENDED_DATA_MODE : SIMPLE_DATA_MODE;

    if (!attr_u32(attrs[info_attr], csi.ext_info) ||
        !attr_u16(attrs[tx_attr], csi.tx_idx) ||
        !attr_u16(attrs[rx_attr], csi.rx_idx) ||
        !attr_u8(attrs[mode_attr], csi.rx_mode))
        return false;
    csi.presence_flags |= CSI_PRESENT_RX_MODE;

    bool truncated = false;
    size_t i_count = 0;
    size_t q_count = 0;
    if (!read_nested_s16(attrs[i_attr], csi.data_i, i_count) ||
        !read_nested_s16(attrs[q_attr], csi.data_q, q_count))
        return false;

    if (i_count != q_count)
        return false;

    if (!extended_abi && attrs[STAGE2_DATA_CH_BW])
    {
        if (!attr_u8(attrs[STAGE2_DATA_CH_BW], csi.ch_bw))
            return false;
    }
    else
    {
        // Legacy ABIs do not export receiver channel width. Use the per-packet
        // data width and mark it as inferred instead of silently reporting 20.
        csi.ch_bw = csi.data_bw;
        csi.metadata_flags |= CSI_META_CH_BW_INFERRED;
    }

    if (extended_abi)
    {
        u32 explicit_count = 0;
        if (!attr_u32(attrs[EXTENDED_DATA_NUM], explicit_count) ||
            explicit_count == 0 || explicit_count > i_count ||
            explicit_count > csi.data_i.size())
            return false;

        csi.data_num = static_cast<u16>(explicit_count);
        csi.metadata_flags |= CSI_META_EXTENDED_ABI;
        if (!attr_u32(attrs[EXTENDED_DATA_CHAIN_INFO], csi.chain_info))
            return false;
        csi.presence_flags |= CSI_PRESENT_CHAIN_INFO;
    }
    else
    {
        if (attrs[STAGE2_DATA_NUM])
        {
            u32 explicit_count = 0;
            if (!attr_u32(attrs[STAGE2_DATA_NUM], explicit_count) ||
                explicit_count == 0 || explicit_count > i_count ||
                explicit_count > csi.data_i.size())
                return false;
            csi.data_num = static_cast<u16>(explicit_count);
        }
        else
        {
            // Nullcon always serializes all 256 storage slots and does not
            // export firmware data_num. Infer the FFT span from BW. This fixes
            // the old always-61-point output at 40/80 MHz.
            const size_t expected = csi_fft_count_for_bandwidth(csi.data_bw);
            const size_t inferred = std::min(expected, i_count);
            if (inferred == 0)
                return false;
            csi.data_num = static_cast<u16>(inferred);
            csi.metadata_flags |= CSI_META_DATA_NUM_INFERRED;
            if (expected > i_count)
                truncated = true;
        }

        if (!attr_u32(attrs[SIMPLE_DATA_H_IDX], csi.h_idx))
            return false;
        csi.presence_flags |= CSI_PRESENT_H_IDX;

        if (attrs[STAGE2_DATA_PKT_SN])
        {
            if (!attr_u16(attrs[STAGE2_DATA_PKT_SN], csi.pkt_sn))
                return false;
            csi.presence_flags |= CSI_PRESENT_PKT_SN;
        }
        if (attrs[STAGE2_DATA_SEGMENT_NUM])
        {
            if (!attr_u32(attrs[STAGE2_DATA_SEGMENT_NUM], csi.segment_num))
                return false;
            csi.presence_flags |= CSI_PRESENT_SEGMENT_NUM;
        }
        if (attrs[STAGE2_DATA_REMAIN_LAST])
        {
            if (!attr_u8(attrs[STAGE2_DATA_REMAIN_LAST], csi.remain_last))
                return false;
            csi.presence_flags |= CSI_PRESENT_REMAIN_LAST;
        }
        if (attrs[STAGE2_DATA_TR_STREAM])
        {
            if (!attr_u8(attrs[STAGE2_DATA_TR_STREAM], csi.tr_stream))
                return false;
            csi.presence_flags |= CSI_PRESENT_TR_STREAM;
        }
        if (attrs[STAGE2_DATA_CHAIN_INFO])
        {
            if (!attr_u32(attrs[STAGE2_DATA_CHAIN_INFO], csi.chain_info))
                return false;
            csi.presence_flags |= CSI_PRESENT_CHAIN_INFO;
        }
        if (attrs[STAGE2_DATA_BAND])
        {
            if (!attr_u8(attrs[STAGE2_DATA_BAND], csi.band))
                return false;
            csi.presence_flags |= CSI_PRESENT_BAND;
        }
    }

    if (truncated)
        csi.metadata_flags |= CSI_META_TRUNCATED;

    return true;
}
} // namespace

class MT76APIPrivate
{
public:
    ~MT76APIPrivate()
    {
        clear_csi_list(csi_list);
    }

    static int csi_dump_callback(struct nl_msg *msg, void *arg)
    {
        auto *context = static_cast<DumpContext *>(arg);
        if (!context || !context->netlink || !context->output)
            return NL_SKIP;

        struct nlattr *vendor_data =
            unl_find_attr(context->netlink, msg, NL80211_ATTR_VENDOR_DATA);
        if (!vendor_data)
        {
            std::fprintf(stderr, "CSI reply has no vendor-data attribute\n");
            context->parse_failed = true;
            return NL_SKIP;
        }

        struct nlattr *ctrl_attrs[NUM_MTK_VENDOR_ATTRS_CSI_CTRL] = {};
        if (nla_parse_nested(ctrl_attrs, MTK_VENDOR_ATTR_CSI_CTRL_MAX,
                             vendor_data, nullptr) < 0 ||
            !ctrl_attrs[MTK_VENDOR_ATTR_CSI_CTRL_DATA])
        {
            std::fprintf(stderr, "CSI reply has malformed control attributes\n");
            context->parse_failed = true;
            return NL_SKIP;
        }

        auto *entry = new (std::nothrow) csi_data();
        if (!entry)
        {
            context->parse_failed = true;
            return NL_SKIP;
        }

        if (!parse_csi_data(ctrl_attrs[MTK_VENDOR_ATTR_CSI_CTRL_DATA], *entry))
        {
            std::fprintf(stderr, "CSI reply has missing or malformed data attributes\n");
            context->parse_failed = true;
            delete entry;
            return NL_SKIP;
        }

        entry->channel_frequency_mhz = context->channel_frequency_mhz;
        entry->presence_flags |= CSI_PRESENT_CHANNEL_FREQ;
        if (context->frequency_is_primary)
            entry->metadata_flags |= CSI_META_FREQ_IS_PRIMARY;
        if (context->tone_masked_reordered)
            entry->metadata_flags |= CSI_META_TONE_MASKED_REORDERED;
        if (!(entry->metadata_flags & CSI_META_CH_BW_INFERRED) &&
            entry->ch_bw != context->channel_bandwidth)
        {
            std::fprintf(stderr,
                         "CSI channel width disagrees with nl80211 interface state\n");
            context->parse_failed = true;
            delete entry;
            return NL_SKIP;
        }

        entry->host_timestamp_ns = static_cast<u64>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count());

        context->output->push_back(entry);
        return NL_SKIP;
    }

    std::vector<csi_data *> *motion_detection_dump(const char *wifi, int packet_count)
    {
        if (!wifi || packet_count < 0 || packet_count > CSI_MAX_DUMP_PACKETS)
        {
            errno = EINVAL;
            return nullptr;
        }

        const unsigned if_index = if_nametoindex(wifi);
        if (!if_index)
        {
            std::fprintf(stderr, "Unknown Wi-Fi interface '%s': %s\n", wifi,
                         std::strerror(errno));
            return nullptr;
        }

        u16 channel_frequency_mhz = 0;
        u8 channel_bandwidth = 0;
        bool frequency_is_primary = false;
        const int frequency_ret = read_interface_channel(
            if_index, channel_frequency_mhz, channel_bandwidth,
            frequency_is_primary);
        if (frequency_ret < 0)
        {
            std::fprintf(stderr,
                         "Cannot read the current channel for '%s': %s\n",
                         wifi, std::strerror(-frequency_ret));
            clear_csi_list(csi_list);
            return nullptr;
        }
        const bool channel_epoch_changed =
            channel_state_valid &&
            (channel_frequency_mhz != last_channel_frequency_mhz ||
             channel_bandwidth != last_channel_bandwidth ||
             frequency_is_primary != last_frequency_is_primary);

        clear_csi_list(csi_list);
        int remaining = packet_count;
        while (remaining > 0)
        {
            struct unl netlink = {};
            if (unl_genl_init(&netlink, "nl80211") < 0)
            {
                std::fprintf(stderr, "Failed to connect to nl80211\n");
                clear_csi_list(csi_list);
                return nullptr;
            }

            struct nl_msg *msg = unl_genl_msg(&netlink, NL80211_CMD_VENDOR, true);
            if (!msg)
            {
                unl_free(&netlink);
                clear_csi_list(csi_list);
                return nullptr;
            }

            const u16 request_count = static_cast<u16>(
                std::min(remaining, CSI_DUMP_PER_REQUEST));
            if (nla_put_u32(msg, NL80211_ATTR_IFINDEX, if_index) ||
                nla_put_u32(msg, NL80211_ATTR_VENDOR_ID, MTK_NL80211_VENDOR_ID) ||
                nla_put_u32(msg, NL80211_ATTR_VENDOR_SUBCMD,
                            MTK_NL80211_VENDOR_SUBCMD_CSI_CTRL))
            {
                nlmsg_free(msg);
                unl_free(&netlink);
                clear_csi_list(csi_list);
                return nullptr;
            }

            struct nlattr *data =
                nla_nest_start(msg, NL80211_ATTR_VENDOR_DATA | NLA_F_NESTED);
            if (!data ||
                nla_put_u16(msg, MTK_VENDOR_ATTR_CSI_CTRL_DUMP_NUM, request_count))
            {
                nlmsg_free(msg);
                unl_free(&netlink);
                clear_csi_list(csi_list);
                return nullptr;
            }
            nla_nest_end(msg, data);

            DumpContext context{&netlink, &csi_list, channel_frequency_mhz,
                                channel_bandwidth, frequency_is_primary,
                                tone_masked_reordered, false};
            const int ret = unl_genl_request(&netlink, msg, csi_dump_callback,
                                             &context);
            unl_free(&netlink);
            if (ret < 0 || context.parse_failed)
            {
                if (ret < 0)
                    std::fprintf(stderr, "nl80211 CSI dump failed: %s\n",
                                 std::strerror(-ret));
                else
                    std::fprintf(stderr,
                                 "Discarding CSI batch after malformed reply\n");
                clear_csi_list(csi_list);
                return nullptr;
            }

            remaining -= request_count;
        }

        u16 final_frequency_mhz = 0;
        u8 final_channel_bandwidth = 0;
        bool final_frequency_is_primary = false;
        const int final_frequency_ret = read_interface_channel(
            if_index, final_frequency_mhz, final_channel_bandwidth,
            final_frequency_is_primary);
        if (final_frequency_ret < 0 ||
            final_frequency_mhz != channel_frequency_mhz ||
            final_channel_bandwidth != channel_bandwidth ||
            final_frequency_is_primary != frequency_is_primary)
        {
            std::fprintf(stderr,
                         "Channel changed or became unreadable while dumping CSI; "
                         "discarding the batch\n");
            clear_csi_list(csi_list);
            errno = final_frequency_ret < 0 ? -final_frequency_ret : EAGAIN;
            return nullptr;
        }

        last_channel_frequency_mhz = final_frequency_mhz;
        last_channel_bandwidth = final_channel_bandwidth;
        last_frequency_is_primary = final_frequency_is_primary;
        channel_state_valid = true;
        if (channel_epoch_changed)
        {
            std::fprintf(stderr,
                         "Channel changed between CSI polls; drained and "
                         "discarded the first batch of the new radio epoch\n");
            clear_csi_list(csi_list);
            errno = EAGAIN;
            return nullptr;
        }

        return &csi_list;
    }

    static int add_csi_config(struct nl_msg *msg, u8 mode, u8 type,
                              u8 value1, u32 value2)
    {
        struct nlattr *config =
            nla_nest_start(msg, MTK_VENDOR_ATTR_CSI_CTRL_CFG | NLA_F_NESTED);
        if (!config)
            return -EMSGSIZE;

        if (nla_put_u8(msg, MTK_VENDOR_ATTR_CSI_CTRL_CFG_MODE, mode) ||
            nla_put_u8(msg, MTK_VENDOR_ATTR_CSI_CTRL_CFG_TYPE, type) ||
            nla_put_u8(msg, MTK_VENDOR_ATTR_CSI_CTRL_CFG_VAL1, value1) ||
            nla_put_u32(msg, MTK_VENDOR_ATTR_CSI_CTRL_CFG_VAL2, value2))
        {
            return -EMSGSIZE;
        }

        nla_nest_end(msg, config);
        return 0;
    }

    static int set_csi(unsigned if_index, u8 mode, u8 type,
                       u8 value1, u32 value2)
    {
        struct unl netlink = {};
        if (unl_genl_init(&netlink, "nl80211") < 0)
            return -ENOLINK;

        struct nl_msg *msg = unl_genl_msg(&netlink, NL80211_CMD_VENDOR, false);
        if (!msg)
        {
            unl_free(&netlink);
            return -ENOMEM;
        }

        if (nla_put_u32(msg, NL80211_ATTR_IFINDEX, if_index) ||
            nla_put_u32(msg, NL80211_ATTR_VENDOR_ID, MTK_NL80211_VENDOR_ID) ||
            nla_put_u32(msg, NL80211_ATTR_VENDOR_SUBCMD,
                        MTK_NL80211_VENDOR_SUBCMD_CSI_CTRL))
        {
            nlmsg_free(msg);
            unl_free(&netlink);
            return -EMSGSIZE;
        }

        struct nlattr *data =
            nla_nest_start(msg, NL80211_ATTR_VENDOR_DATA | NLA_F_NESTED);
        if (!data)
        {
            nlmsg_free(msg);
            unl_free(&netlink);
            return -EMSGSIZE;
        }

        const int config_ret = add_csi_config(msg, mode, type, value1, value2);
        if (config_ret < 0)
        {
            nlmsg_free(msg);
            unl_free(&netlink);
            return config_ret;
        }
        nla_nest_end(msg, data);

        const int ret = unl_genl_request(&netlink, msg, nullptr, nullptr);
        unl_free(&netlink);
        if (ret < 0)
            std::fprintf(stderr, "nl80211 CSI configuration failed: %s\n",
                         std::strerror(-ret));
        return ret;
    }

    int motion_detection_start(const char *wifi)
    {
        if (!wifi)
            return -EINVAL;

        const unsigned if_index = if_nametoindex(wifi);
        if (!if_index)
            return errno ? -errno : -ENODEV;

        tone_masked_reordered = false;
        channel_state_valid = false;
        int ret = set_csi(if_index, 2, 3, 0, 34); // QoS data frames.
        if (!ret)
            ret = set_csi(if_index, 2, 5, 2, 0); // Mask and reorder tones.
        if (!ret)
            ret = set_csi(if_index, 2, 9, 1, 0); // Firmware event output.
        if (!ret)
            ret = set_csi(if_index, 1, 0, 0, 0); // Start capture.

        if (ret < 0)
            (void)set_csi(if_index, 0, 0, 0, 0); // Best-effort rollback.
        else
            tone_masked_reordered = true;
        return ret;
    }

    int motion_detection_stop(const char *wifi)
    {
        if (!wifi || !*wifi)
            return 0;

        const unsigned if_index = if_nametoindex(wifi);
        if (!if_index)
            return errno ? -errno : -ENODEV;
        const int ret = set_csi(if_index, 0, 0, 0, 0);
        if (ret >= 0)
        {
            tone_masked_reordered = false;
            channel_state_valid = false;
        }
        return ret;
    }

private:
    std::vector<csi_data *> csi_list;
    bool tone_masked_reordered = false;
    bool channel_state_valid = false;
    u16 last_channel_frequency_mhz = 0;
    u8 last_channel_bandwidth = 0;
    bool last_frequency_is_primary = false;
};

MT76API::MT76API() : d(new MT76APIPrivate()) {}

MT76API::~MT76API()
{
    delete d;
}

std::vector<csi_data *> *MT76API::motion_detection_dump(const char *wifi,
                                                         int packet_count)
{
    return d->motion_detection_dump(wifi, packet_count);
}

int MT76API::motion_detection_start(const char *wifi)
{
    return d->motion_detection_start(wifi);
}

int MT76API::motion_detection_stop(const char *wifi)
{
    return d->motion_detection_stop(wifi);
}
