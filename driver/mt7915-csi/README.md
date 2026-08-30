# AX3000T CSI Driver · Stage 2

这一阶段只做一件事：把 Nullcon 已验证的 MT7981/MT7915 CSI 路径，整理成一份可以审计、可以离线复现、并且不会因为坏包拖垮路由器的驱动补丁。

它**没有写入路由器、没有重启无线、没有修改 MtkCSIdump**。本目录是构建输入，不是可直接刷写的固件。

## 交付物

- `patches/0001-mt7915-csi-v2-hardened.patch`：面向 `mt76` commit `39c960c3ada558b4c2e7915772483d3731573d09` 的完整补丁。
- `ABI.md`：内核到用户态的 netlink ABI；原有属性 0–19 保持不变，新能力只从 20 往后追加。
- `tests/verify.py`：不需要 AX3000T 的离线验证器，检查基线、补丁可应用性、ABI 编号和异常 TLV 模型。
- `patches/reference-nullcon-forward-port.patch`：研究阶段的原始 forward-port，仅保留作差异证据；构建请使用 hardened 补丁。
- `SOURCES.md`：所有上游版本、来源和校验值；不要靠“最新版”复现驱动。
- `reproduction/`：Stage 2 的离线 ABI 复现记录、Docker 构建脚本和公开镜像闸门。它证明 hardened 补丁能在官方 SDK 中编译，同时也证明该 SDK 的 `.gnu.linkonce.this_module=0x280`、APK 和内核依赖与 Kwrt 的 `0x440`、IPK 不兼容；SDK 产物只能作编译对照，绝不能安装或塞进固件镜像。

## 这版解决了什么

### 1. 不再把固件事件强制解释成固定 C 结构体

固件上报的是 TLV。补丁逐项验证 header、payload 长度、重复 tag、I/Q 数量和 20/40/80 MHz 上限，再复制数据。截断、奇数字节、超长数组、错误 band 和矛盾 count 都会被丢弃，不会越界读取或写入。

MT7981 固件存在两种向量形式：按带宽裁短，或总是填充到 256 个复数点。驱动接受两者，但导出的有效点数固定为：

| 信道带宽 | 有效复数点 |
|---:|---:|
| 20 MHz | 64 |
| 40 MHz | 128 |
| 80 MHz | 256 |

因此用户态不需要再“猜 61 个点是不是驱动限制”。去掉空子载波后看到 61 点，是 20 MHz 的常见后处理结果；内核 ABI 本身明确给出 `CH_BW` 和 `DATA_NUM`。

### 1.1 80 MHz 固件分段不是一条用户态记录

MediaTek 25.12 的 connac2 实现明确在 MT798x/MT7916 的 80 MHz 路径使用 `segment_num + remain_last`。固件可以把一个 256 点 chain 拆成 First/Middle/Last 多个 MCU 事件；单个事件的 `data_num` 因而可以小于 256，即使 I/Q TLV 在 wire 上仍填充到 256 个槽位。

本补丁为每个 PHY 保存一个有界的在途 chain：

- First 必须从 segment 0 开始，并带有效的 16-bit `PKT_SN`。
- Middle/Last 必须与 First 的 packet、chain、TA、band、tx/rx index、带宽和 stream 一致，segment 必须严格加一。
- 每段 I/Q 长度和 count 单独验证，累计绝不超过 256。
- 错序、跨包、跨 chain、缺段或溢出会清空在途状态并增加 `malformed`；下一条合法 First 可以立即恢复。
- 只有 Last 到达且累计恰好为 256 点时才进入导出队列。中间 MCU 事件不会成为 netlink/UDP 记录。

官方证据来自 MediaTek `mtk-openwrt-feeds` 的 `0099-cp-mtk-mt76-mt7915-add-connac2-support.patch`；精确提交和路径记录在 `SOURCES.md`。其中原始函数名为 `csi_integret_segment_data`。这里保留其 wire 语义，同时补上 packet/TA/band、累计长度和失败恢复校验。

### 2. 多天线/多链分析所需元数据不再丢失

ABI v2 追加导出：信道带宽、有效点数、包序号、分段号、剩余分段、收发流、chain 信息和 band。旧属性编号完全保留；旧消费者仍能读取原字段，新消费者可以用 `PKT_SN + TS + TA + BAND + CHAIN_INFO + RX_ANT` 做同包链路归组。旧固件没有的可选 TLV 不会伪装成 0，而是直接不导出对应属性。

这些字段为后续 AoA/扇区概率模型提供必要观测，但不等于已经获得校准后的绝对角度。原装 AX3000T 仍需天线映射、固定相位差和房间基线标定。

### 3. 控制面和队列更稳健

- MAC 嵌套属性按索引 0–5 写入，拒绝重复、缺失、越界和非法地址。
- `VAL2` 同时兼容历史 `u8` 和规范 `u32` 编码。
- 只有 MCU 确认命令成功后才更新本地状态。
- 队列上限从 3000 条降到 512 条；满时丢最旧数据并计数，避免采集端停读后长期吃掉路由器内存。
- netlink 编码在锁外完成；失败时只在 CSI 仍启用且有容量时回队。
- 时间间隔比较使用无符号差值，可跨 32-bit 时间戳回绕。
- tone mask/reorder 全部受 `data_num` 边界保护，并修正 `memmove` 少乘 `sizeof(s16)` 的字节数错误。

## 离线验证

```sh
python3 driver/mt7915-csi/tests/verify.py --model-only
```

要同时验证补丁能精确应用到固定 mt76 基线：

```sh
python3 driver/mt7915-csi/tests/verify.py \
  --baseline /path/to/mt76-at-39c960c3
```

完整验证要求基线 HEAD 精确等于 `39c960c3…`，然后在临时目录应用补丁；不会改动原始仓库。也可用环境变量 `MT76_BASELINE` 指定路径。

## 构建与上机闸门

本阶段通过的是**源码与 ABI 闸门**，不是上机闸门。进入部署前仍必须依次满足：

1. 用当前路由器对应的 OpenWrt/Kwrt 25.12、Linux 6.12.94 构建环境编译。
2. 新旧模块 `vermagic`、架构和依赖符号完全一致。
3. 先产出可恢复的 kmod 包，并把原模块和已下载的官方回滚包放在路由器本地。
4. 只做模块级替换；不碰 MTD、bootloader、Factory/NVRAM，也不先制作“整机一把梭”镜像。
5. 维护 Mac 的 Wi‑Fi/VPN 为默认互联网路径；AX3000T 以太网接口不设置默认网关和 DNS。
6. 首次无线重载必须在明确的维护窗口进行，并准备串口/有线回退。

## 明确边界

- 支持范围刻意限定为 20/40/80 MHz；160 MHz 分段重组没有足够的 AX3000T 实机证据，本补丁会拒绝它。
- 每个 PHY 只允许一个在途 80 MHz chain，状态内存固定有界；如果固件实际交错发送两个未完成 chain，后到的 First 会使前一个记为 malformed，而不会把两者拼在一起。
- CSI 能给出相对相位、幅度、链路和时间变化；它不能仅靠一台未校准路由器稳定输出“厘米级位置”。
- 距离优先做分区/接近概率或经房间标定的回归。仅凭 OFDM 子载波相位斜率做绝对 ToF，容易被包间相位偏移和多径误导。
- 角度优先做左/中/右或更多扇区概率；绝对 AoA 需要链映射、天线几何和固定相位校准。

这条阶段线的目标不是制造夸张效果，而是让后续采集、校准和定位算法建立在可解释的数据上。
