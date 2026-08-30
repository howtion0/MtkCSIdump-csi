# AX3000T CSI 的玩法边界

## 结论

原装 AX3000T 的可辩护路线是：**严格同包双 Rx CSI → 校准后的粗角度
normalized support；本地标注 → near/mid/far support；增加物理分离且时间对齐
的 receiver → 二维 fused-support heatmap。**

固定频道、双阵元 CSI 不会自动变成绝对 ToF、无歧义 360° 方位、厘米轨迹或
人体轮廓。神经网络只能学习已有数据分布，不能补出缺失的阵列孔径、带宽和
同步。

## 能力矩阵

| 目标 | 单 AX3000T | 加本地标定/标签 | 加多个独立 receiver |
|---|---|---|---|
| 复数 CSI | Stage 2 type-5 canonical path可采 | 可测稳定性 | 增加空间链路 |
| 靠近/远离 | 原始幅度/RSSI 会变但混杂严重 | near/mid/far labeled kNN | 多点一致性更强 |
| 左/中/右 | 原始链间相位不可直接信 | 同 PPDU + 天线坐标 + 硬件相位校准后可实验 | 可交叉验证 |
| 13/17/25 扇区 | UI 可分桶 | 分桶更细不等于分辨率提高 | 几何交汇可减少歧义 |
| 稳定 360° | 两阵元前后镜像，不可 | 单基线仍不可完全消除 | 非共线多点有机会消歧 |
| 绝对距离 | 固定频道 CSI 不可 | 只能做环境标签代理 | 仍不是 ToF |
| FTM 距离 | 当前 AX 路径无已验证 PMSR/FTM | 未来可接真实 FTM | 可作独立输入，但不能伪造 |
| 单机二维坐标 | 欠约束 | 指纹强依赖房间/设备 | 多 receiver 只能输出模型支持热力图 |
| 人体骨架/呼吸 | 当前输入与真值不足 | 需专门多链路、同步真值和评测 | 本实现不声称具备 |

## 两阵元 AoA 的硬边界

相位是天线**有向基线**与传播方向的投影：

```text
Δφ(f) = -2π f · dot(p_target - p_reference, u_arrival) / c   (mod 2π)
```

所以必须知道两个 `rx_idx` 对应哪根天线、相位中心坐标、阵列 broadside、Tx
和 transport stream。只保存正的 `spacing` 会丢掉左右符号。本实现使用完整
二维向量，并要求基线至少 20 mm、在 15° 内垂直 broadside。20 mm 是排除
1 mm 等明显伪几何的 sanity floor，不是对原装 AX3000T 天线间距的测量结论；
真实坐标仍必须拆机/测量后写入映射。

0° broadside 校准对反转链/坐标完全不敏感，不能验左右；因此实现强制两个
物理独立 capture：一个拟合点和一个 opposite-side holdout。相同 CSI samples
即便换 session ID、hash 声明、时间戳或包序号仍被拒绝。两点均须
`10° ≤ |angle| ≤ 75°`、符号相反、相隔至少 20°；holdout 不参与拟合，只用于
cross-angle residual 门。反转链、复用同一 capture、相位状态变化或明显静态
多径不一致都会失败。两个角度通过仍不是全房间多径正确性的证明。

在 5 GHz，波长约 5–6 cm。间距大于半波长会出现多个相位绕回候选；一条
线性基线还有固有前后镜像。只有两个空间阵元，无法像 3/4/8/16 阵元系统那样
稳健拆分直达路径与多个反射路径。本实现因此采用 conventional Bartlett 粗
扫描，列出栅瓣候选；不把它命名为 MUSIC。

## tone、频率与 segment 边界

真实 AoA/CIR 只接受 Stage 2 `TONE_MASKED_REORDERED` 的 canonical 64/128/256
tone order，同时拒绝 inferred BW/count、未知 quality/presence 位、未知
`rx_mode` 以及 capture 内 mode/profile 变化。当前仅接受 Stage 2 type-5 switch
明确处理的 OFDM/HT/VHT/HE-SU profile。even-N tone 坐标是
`-N/2 … N/2-1`，不是偏半个子载波的
`-(N-1)/2 … +(N-1)/2`。

40/80 MHz 若只有 primary frequency，无法唯一建立中心频率与 tone 坐标，
所以拒绝。当前还保守要求 `channel_bw == data_bw`；首轮实测固定 20 MHz。
Stage 2 已重组 80 MHz，最终 segment 只是 provenance，绝不再拼接。

## relative CIR 为什么不是距离

20 MHz 的原始时延分辨率量级为 `1/B = 50 ns`，单程光程约 15 m。零填充只
插值，不创造带宽。packet detection delay、采样偏移、CFO/SFO 和未知相位零点
又会平移/扭曲 CIR。因此每包先 IFFT、按最强峰对齐，再做 non-coherent
power average；只报告相对多径曲线、RMS delay spread 与次峰相对延迟，禁止
把峰位置乘光速显示成手机距离。

Chronos/Splicer 使用跨频拼接扩展有效带宽；SpotFi 的 joint ToF 也是相对路径
参数。当前固定频道 AX 数据不具备这些输入。

## 距离代理与设备更换

kNN 的 near/mid/far 是本地标签定义，不是物理测距。特征中的 `rssi_raw` 和
`snr_raw` 是固件字节，未声明 dBm/dB。模型会被手机芯片、发射功率、握持姿态、
家具/门、人遮挡、频道/MCS、温漂和重启改变。

模型内容 ID 绑定训练 feature/label、room/device 与训练 source 内容 ID；但
source ID 和标签仍是操作者声明，不会自动证明真值正确。未知手机、未知房间
或远离训练特征会降低 evidence
并产生 `unseen_transmitter_device`、`room_domain_shift`、
`feature_domain_shift`、`out_of_distribution`。换设备应补录所有标签和多个
姿态；不能通过调高 UI 数值“修复”。裸向量预测带 `not_capture_bound`，只有
绑定同一 capture manifest、receiver、TA、radio profile 与实际时间窗的特征
才能参与 AoA 融合。

## 多 receiver 二维 heatmap 的边界

每个双阵元 receiver 形成长扇区与前后双叶。空间分离的多个 receiver 可把
normalized support 在网格上组合，得到更集中的显示区域，但这不是校准过的
Bayesian posterior。因此名称是 fused support、display peak 和 80% display-
mass radius，不叫 likelihood/MAP/credible radius。

融合前必须满足：

- receiver ID 与 calibration artifact hash 唯一，坐标至少分离 25 cm；
- 同一 TA、兼容 radio/tone config；
- AoA 使用的实际记录窗重叠，并且明确属于同一个对齐 timebase；每个窗从两端
  扣除声明的 clock uncertainty 后默认至少还要重叠 1 ms；
- 每台 receiver 的最大 clock uncertainty 不超过门限；
- 零证据拒绝、低证据忽略，没有隐形 `0.1` 权重下限。

不同路由器的 wall-clock 数值相近不证明同步。没有 PTP/共同采集时基与测得
clock uncertainty 时，Stage 3 拒绝多点融合。一个路由器内部两根天线也不能
冒充两个房间级 receiver。

manifest 中的 timebase ID/uncertainty，以及 receiver 坐标、heading、room ID
都是操作者提供的 provenance，程序只
能检查字段一致性、数值门限和时间窗，不能从 CSI 数据独立证明两个主机已经
同步。相同字符串不是同步证据；发布实测结果时还要附 PTP/触发/时钟测量记录。

## RuView 风格展示可以做到哪里

可以漂亮地展示 3D 房间、真实 Tx–receiver 布局、动态链路、CSI 波纹、扇区
support、相对 CIR 和半透明 fused-support 云。波纹/粒子/“能量团”属于
decorative layer，必须标注；热力图只代表模型支持，不是被观测到的肉体。

建议 UI 明确分层：

1. **Measured**：CSI2、完整 stream key、raw metrics、loss/quality flags；
2. **Derived**：phase concentration、粗 sectors、relative CIR、range label；
3. **Fused support**：多 receiver 热力图与 display-mass radius；
4. **Synthetic/decorative**：粒子与波纹，永远保留醒目标识。

## 实测发布门槛

任何“已实现方位/距离”截图至少应同时保存：固件/驱动/source hash、session
manifest、频道/BW/频率/tone flags、两天线映射与照片、signed-angle calibration、
boot/radio epoch、严格配对率与所有硬失败、timebase/clock uncertainty、真值
点位、设备/姿态/房间、跨日/重启/换手机误差和原始 capture hash。

合成测试只证明数学与软件可复现，不能替代这些硬件证据。
