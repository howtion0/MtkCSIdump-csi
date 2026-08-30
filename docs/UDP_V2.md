# CSI UDP protocol v2

UDP v2 carries one firmware CSI observation per datagram. Multi-chain PPDU
pairing happens at the receiver; a datagram never concatenates observations
from unrelated timestamps.

All multibyte integers and signed IQ16 values are **big-endian (network byte
order)**. The fixed header is 80 bytes, followed by exactly `data_num` pairs of
`int16 I, int16 Q`.

## Header

| Offset | Size | Field | Meaning |
| ---: | ---: | --- | --- |
| 0 | 4 | magic | ASCII `CSI2` |
| 4 | 1 | version | `2` |
| 5 | 1 | quality flags | inferred/truncated/ABI flags below |
| 6 | 2 | header length | `80` |
| 8 | 4 | message length | header + payload |
| 12 | 4 | sequence | host UDP sequence, wraps modulo 2³² |
| 16 | 8 | host timestamp ns | Unix wall-clock time after netlink receipt |
| 24 | 4 | driver timestamp | firmware/driver 32-bit timestamp |
| 28 | 4 | ext info | driver CSI `INFO` field |
| 32 | 4 | h index | legacy CSI `h_idx`, see presence bitmap |
| 36 | 4 | chain info | chain metadata, see presence bitmap |
| 40 | 6 | transmitter address | 802.11 TA/MAC |
| 46 | 1 | RSSI | signed dBm-like driver value |
| 47 | 1 | SNR | unsigned driver value |
| 48 | 1 | channel BW | driver BW enum; may be inferred |
| 49 | 1 | data BW | per-packet BW enum |
| 50 | 1 | primary channel index | relative index, not IEEE channel number |
| 51 | 1 | band | driver band index; see presence bitmap |
| 52 | 1 | RX mode | driver PHY receive-mode enum |
| 53 | 1 | rate MCS | `0xff` when absent |
| 54 | 2 | TX index | firmware transmit-chain/index value |
| 56 | 2 | RX index | firmware receive-chain/index value |
| 58 | 2 | data count | complex IQ pairs in this datagram |
| 60 | 2 | center frequency MHz | zero when absent |
| 62 | 1 | sample format | `1` = signed IQ16 |
| 63 | 1 | rate NSS | zero when absent |
| 64 | 2 | packet sequence number | firmware `pkt_sn`, see presence bitmap |
| 66 | 4 | segment number | firmware segment number |
| 70 | 4 | rate kbit/s | zero when absent |
| 74 | 2 | presence bitmap | tells whether optional zero-valued fields exist |
| 76 | 1 | remain/last | firmware segment completion value |
| 77 | 1 | transport stream | firmware `tr_stream` |
| 78 | 1 | guard interval | `0xff` when absent |
| 79 | 1 | reserved | zero |

## Quality flags (offset 5)

| Bit | Name | Meaning |
| ---: | --- | --- |
| 0 | `CH_BW_INFERRED` | channel BW copied from data BW |
| 1 | `DATA_NUM_INFERRED` | count derived from data BW |
| 2 | `EXTENDED_ABI` | MediaTek shifted extended ABI decoded |
| 3 | `TRUNCATED` | source advertised more bins than the ABI carried |

## Presence bitmap (offset 74)

| Bit | Field known to be present |
| ---: | --- |
| 0 | `h_idx` |
| 1 | `chain_info` |
| 2 | `pkt_sn` |
| 3 | `segment_num` |
| 4 | `remain_last` |
| 5 | `tr_stream` |
| 6 | RX mode |
| 7 | rate MCS |
| 8 | rate NSS |
| 9 | rate kbit/s |
| 10 | channel center frequency |
| 11 | band index |

Optional firmware values can legitimately be zero. Consumers must inspect this
bitmap rather than treating zero as missing.

Stage-2 performs 80 MHz segment reassembly inside the driver. First/Middle
reports do not reach UDP; the completed Last report is exported once with
`data_num=256`, the final firmware `segment_num`, and `remain_last=0`. These
fields are provenance/grouping evidence and must not trigger a second I/Q
concatenation.

## Limits and validation

- `1 <= data_num <= 1024`.
- `message length == 80 + data_num × 4`.
- Total datagram length must not exceed 65,507 bytes.
- Unknown sample formats and mismatched lengths are rejected.
- Legacy UDP v1 has no magic and uses host-native little-endian doubles in the
  historical implementation. The Python decoder supports it only for migration;
  new senders must use v2.
