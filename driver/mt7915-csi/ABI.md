# MT7915 CSI netlink ABI v2

Vendor ID 为 `0x0ce7`，CSI subcommand 为 `0xc2`。v2 的兼容原则是：**0–19 不改编号、不换含义；新字段只追加。**

## CSI data attributes

| ID | 内核名称 | 类型 | 含义 |
|---:|---|---|---|
| 0 | `UNSPEC` | — | 未使用 |
| 1 | `PAD` | — | 对齐保留 |
| 2 | `VER` | u8 | ABI 版本；本补丁输出 2 |
| 3 | `TS` | u32 | 固件接收时间戳；单位由固件定义，允许回绕 |
| 4 | `RSSI` | u8/s8 bits | 接收强度的 8-bit 二补码表示 |
| 5 | `SNR` | u8 | 信噪指标 |
| 6 | `BW` | u8 | 当前数据/PPDU 带宽枚举 |
| 7 | `CH_IDX` | u8 | 主 20 MHz 信道位置索引 |
| 8 | `TA` | nested u8[6] | 发射端 MAC；子属性索引 0–5 |
| 9 | `I` | nested u16[`DATA_NUM`] | 有符号 s16 I，按原始 bit pattern 放入 u16 |
| 10 | `Q` | nested u16[`DATA_NUM`] | 有符号 s16 Q，按原始 bit pattern 放入 u16 |
| 11 | `INFO` | u32 | 固件扩展信息 |
| 12–15 | `RSVD1`…`RSVD4` | — | 历史保留 |
| 16 | `TX_ANT` | u16 | 固件 tx index |
| 17 | `RX_ANT` | u16 | 固件 rx index |
| 18 | `MODE` | u8 | PHY 接收模式 |
| 19 | `H_IDX` | u32 | 兼容字段；v2 中仍携带 chain info |
| 20 | `CH_BW` | u8 | 信道带宽：0=20、1=40、2=80 MHz |
| 21 | `NUM` | u32 | 本记录有效 I/Q 复数点数：64/128/256 |
| 22 | `PKT_SN` | u16，可选 | 固件包序号；按 16 bit 回绕 |
| 23 | `SEGMENT_NUM` | u32，可选 | 固件分段号；未分段时通常为 0 |
| 24 | `REMAIN_LAST` | u8，可选 | 还有后续分段时为 1 |
| 25 | `TR_STREAM` | u8，可选 | 固件收发流元数据 |
| 26 | `CHAIN_INFO` | u32 | 完整 chain 信息；bit 15 沿用“最后一条 chain”语义 |
| 27 | `BAND` | u8 | 固件 radio band 索引，AX3000T 为 0 或 1 |

`I` 与 `Q` 必须长度相同，并且只输出 `NUM` 个元素。消费者不得再把 256 写死，也不应从数组长度反推带宽。

## Firmware segment、netlink record 与 UDP record

这三个层次不能混为一谈：

```text
多个 firmware MCU segment
        ↓ 内核验证并重组
一个 netlink CSI record（80 MHz 时 NUM=256）
        ↓ MtkCSIdump 编码
一个 UDP CSI v2 record
```

80 MHz 的 First/Middle 不导出。Last 到达、累计恰好 256 点后，属性 `SEGMENT_NUM` 保留最后一个固件 segment 编号，`REMAIN_LAST` 为 0；这只是来源诊断信息，并不表示 UDP 数据包还需要再次拼接。UDP 消费者应把每条成功解码的记录当作完整 CSI chain。

20/40 MHz 不做跨事件拼接，仍兼容裁短 TLV 和填充到 256 槽位的两种固件格式。其导出 `NUM` 分别为 64 和 128。

## 固件事件 TLV

固件 TLV header 为两个 little-endian u32：`tag` 与 payload `len`。本补丁识别 0–23：

| Tag | 字段 | Payload |
|---:|---|---|
| 0 | firmware version | le32 |
| 1 | channel bandwidth | le32 |
| 2 | RSSI | le32，使用低 8 bit |
| 3 | SNR | le32，使用低 8 bit |
| 4 | band | le32 |
| 5 | reported CSI count | le32 |
| 6 | I vector | little-endian s16 array |
| 7 | Q vector | little-endian s16 array |
| 8 | data bandwidth | le32 |
| 9 | primary channel index | le32 |
| 10 | transmitter address | 6–8 bytes |
| 11 | extra info | le32 |
| 12 | RX mode/rate word | le32 |
| 13–16 | reserved | safely skipped |
| 17 | chain info | le32 |
| 18 | tx/rx index word | le32 |
| 19 | timestamp | le32 |
| 20 | packet sequence | le32，可选 |
| 21 | bandwidth segment | le32，可选 |
| 22 | remain-last | le32，可选 |
| 23 | tx/rx stream | le32，可选 |

未知的更高 tag 会按已验证的长度跳过，便于未来固件追加字段；已知 tag 重复、标量不是 4 字节、TLV 截断、向量为奇数字节或超过 256 点都会使整条记录失效。

Tag 20–23 在旧固件中可能不存在。驱动只在对应 TLV 确实出现时导出属性 22–25；**属性缺失才表示 unknown**。零是合法的包序号、分段号或流编号，消费者不得把零当作 missing sentinel。

## 同包多链归组建议

优先使用下列键建立候选组：

```text
(BAND, TA, PKT_SN, TS, SEGMENT_NUM)
```

再以 `CHAIN_INFO`、`RX_ANT`、`TX_ANT` 区分空间链。属性 `PKT_SN` 缺失时，只能用 `TS + TA + BAND` 的窄时间窗降级归组，并把结果标成较低置信度。

不要把 `RX_ANT` 直接当成物理天线方位。必须先通过逐天线遮挡/近场激励实验建立 RF chain → 外壳天线映射，再校准链间固定相位。
