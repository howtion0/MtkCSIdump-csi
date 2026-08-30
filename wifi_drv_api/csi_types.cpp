#include "mt76_api.h"

#include <algorithm>

size_t csi_fft_count_for_bandwidth(u8 bandwidth)
{
    switch (bandwidth)
    {
    case 0:
        return CSI_BW20_DATA_COUNT;
    case 1:
        return CSI_BW40_DATA_COUNT;
    case 2:
        return CSI_BW80_DATA_COUNT;
    case 3:
        return CSI_BW160_DATA_COUNT;
    case 4:
        return CSI_BW320_DATA_COUNT;
    default:
        return 0;
    }
}

size_t csi_valid_sample_count(const csi_data &data)
{
    if (data.data_num > 0)
        return std::min<size_t>(data.data_num, data.data_i.size());
    return std::min(csi_fft_count_for_bandwidth(data.data_bw),
                    data.data_i.size());
}

bool csi_validate_dense_indices(const std::vector<size_t> &indices,
                                size_t capacity, size_t &dense_count)
{
    dense_count = 0;
    if (indices.empty() || indices.size() > capacity)
        return false;

    std::vector<bool> seen(capacity, false);
    for (const size_t index : indices)
    {
        if (index >= capacity || seen[index])
            return false;
        seen[index] = true;
    }

    // Netlink permits attributes to arrive out of order, but CSI arrays must
    // still form one dense 0..N-1 index set. A gap is corrupted data.
    for (size_t index = 0; index < indices.size(); ++index)
    {
        if (!seen[index])
            return false;
    }

    dense_count = indices.size();
    return true;
}
