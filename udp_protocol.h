#pragma once

#include "parsers/parser.h"

#include <cstddef>
#include <cstdint>
#include <vector>

namespace csi_udp
{
constexpr uint8_t PROTOCOL_VERSION = 2;
constexpr uint8_t SAMPLE_FORMAT_SIGNED_IQ16 = 1;
constexpr size_t V2_HEADER_SIZE = 80;
constexpr size_t MAX_COMPLEX_SAMPLES = 1024;
constexpr size_t MAX_UDP_DATAGRAM_SIZE = 65507;

// Serialize one CSI observation. All integer fields, including I/Q samples,
// are encoded in network byte order. An empty result means the packet cannot
// be represented by protocol v2.
std::vector<uint8_t> encode_v2(const CsiPacket &packet, uint32_t sequence);
} // namespace csi_udp
