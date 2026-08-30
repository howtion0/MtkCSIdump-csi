# 一手来源到实现选择

这里记录“来源的真实输入条件 → Stage 3 借鉴什么 → 明确不能移植什么”。论文
精度只属于原论文的天线数、带宽、同步、训练域与评测协议，不能贴到双 Rx
AX3000T 上。

| 一手来源 | 原始输入/结论 | Stage 3 采用 | 不移植的边界 |
|---|---|---|---|
| [Nullcon: Unlock hidden Superpowers in MediaTek WiFi Chips](https://nullcon.net/wp-content/uploads/2026/04/Unlock-hidden-Superpowers-in-MediaTek-WiFi-Chips.pdf) / [MtkCSIdump](https://github.com/MtkWifiRev/MtkCSIdump) | 演示材料列出 Xiaomi AX3000T/OpenWrt One；patched `mt76` 经 netlink 接收每包 CSI。第 27 页现场图只展示 2 个 active antennas、61 samples/packet、raw magnitude/phase/FFT | 证明“整机存在可运行 capture 路径”，真实数据入口对齐 CSI2/mt76 | 现场 visualizer 不是定位/动作精度评测；61 点历史形态不能当 canonical tone map，也不能推出第三条 Rx 链 |
| [ZTECSITool / arXiv 2506.16957](https://arxiv.org/abs/2506.16957) / [official repository](https://github.com/WiFiZTE2025/ZTE_WiFi_Sensing) | **ZTE** E2631/SR6110、MT7916、定制云端固件；报告最多 160 MHz、512 tones、6 chains，并显式携带 peer、微秒 timestamp、BW/PHY/MCS/GI/RSSI/AGC | 借鉴显式 PPDU 元数据、链数、tone 数和 bounded UDP schema；说明商业 AP 能做得更完整 | 不是 Xiaomi AX3000T/MT7976CN，也不是可移植的公开固件；其 3×2/160 MHz/512-tone 能力不能嫁接到本机 |
| [SpotFi, SIGCOMM 2015](https://conferences.sigcomm.org/sigcomm/2015/pdf/papers/p269.pdf) | **3 Rx**，跨子载波 joint AoA/ToF，多 AP；ToF 是相对路径参数而非 absolute ToF | 跨 tone steering、必须区分多径、保留歧义 | 不把其定位误差搬到 2 Rx；不把 joint ToF 叫米制距离 |
| [Chronos, NSDI 2016](https://www.usenix.org/system/files/conference/nsdi16/nsdi16-paper-vasisht.pdf) | 跨多个 Wi-Fi 频道拼接，扩展有效带宽以估 ToF | 用于解释时延分辨率由有效带宽决定 | 固定频道 CSI2 没有其跳频相位连续性，不能声称亚米 ToF |
| [Splicer, MobiCom 2015](https://www.sigmobile.org/mobicom/2015/papers/p53-xieA.pdf) | 多频段拼接；20 MHz 原始 CIR 分辨率约 50 ns（约 15 m 单程光程） | CIR 输出 nominal resolution，zero padding 明示仅插值 | 不把 20/40/80 MHz IFFT 峰当绝对距离 |
| [On Phase Offsets of 802.11ac Commodity WiFi](https://ar5iv.labs.arxiv.org/html/2005.03755) | RF 链/PLL 有固定、多状态、随频率变化的相位偏置，必须校准 | 每 tone `H_target/H_reference`、圆均值/concentration；绑定 boot/radio epoch | Intel 9260 的具体状态不假设等同 MT7976；只采用“必须实测”的原则 |
| [CSI preprocessing benchmark](https://arxiv.org/html/2307.12126v2) | 复比值/共轭积可抑制公共相位误差 | 仅对同 PPDU、同 Tx/stream 的双 Rx 做复比值 | 复比值不能消除链间硬件偏置、多径、错误 tone order 或跨包相位 |
| [MonoLoco, MobiSys 2018](https://www.elahe.web.illinois.edu/Elahe%20Soltan_files/papers/MobiSys18_MonoLoco_CameraReady.pdf) | 3 Tx × 3 Rx × 30 = **270 sensing elements**，使用 relative ToF | 用于说明观测维数来自真实链路/tones | 双 Rx 不能复刻其虚拟阵列或多径分离能力 |
| [FUSIC, INFOCOM 2020](https://kjiokeng.github.io/assets/pdf/papers/fusic-infocom20.pdf) | CSI 与真实 **FTM** 测距融合 | 未来接口可接受独立测距证据 | 当前 AX 驱动路径没有已验证 PMSR/FTM，绝不生成虚构 range |
| [Multi-point probability fusion](https://ar5iv.labs.arxiv.org/html/2009.02798) | **2 AP × 4 Rx、80 MHz、监督指纹** | 借鉴多物理接收点网格组合和域标记 | 当前实现只称 normalized/fused support，不引用论文概率校准或精度 |
| [MMP, Sensors 2018](https://www.mdpi.com/1424-8220/18/6/1753) | 依赖更大的二维/虚拟孔径，含仿真及特定数据 | 说明 joint multipath parameters 需要足够孔径 | 不在 1×2 基线上声称二维超级分辨率 |
| [Large-scale learning-based CSI localization](https://arxiv.org/html/2504.17173) | 400+ AP、五层楼、leave-one-smartphone-out；结果仍有设备/楼层域问题 | 保存 room/device domain，确定性测试换手机退化 | 大网络/深模型结果不代表单路由器；神经网络不补几何与同步 |
| [Location Independent HAR using Self-Training, IEEE IoT-J 2025](https://doi.org/10.1109/JIOT.2025.3565384) | 正文是 1 Tx + **两个相距 2 m 的 Raspberry Pi receiver**，距 Tx 约 3 m；3 个环境、5 类活动，依赖空房 reference、少量新环境标签、未标注目标域、LSTM/MMD/pseudo-label。论文表 II 报告 location-independent 95.20% | 借鉴空房基线、显式 domain、置信阈值、自训练与跨环境 holdout 的评测结构 | 不是机内双天线，也不是 zero-shot、方位或距离算法；论文自身对 20/80 MHz、55/30 tones 的叙述需在复现时重新核对，百分比不能迁移到 AX3000T |
| [ESPCAL](https://github.com/xMatrix-Lab/ESPCAL) | 多 ESP32 组成 8/16 阵元协作阵列，专门做路径/阵列校准和 1D/2D MUSIC | 借鉴显式天线坐标、链映射、已知点和校准 provenance | AX3000T 两 Rx 不是 ESPCAL ULA/UPA，不能复刻 2D MUSIC |
| [Wi-Fi multipath parameter estimation code](https://github.com/francescamen/Wi-Fi-multipath-parameter-estimation) | 参考代码主要假设 1×4 与 256 tones | 对照算法需要的输入维度/tone coordinate | 不把 1×4 静默降为 1×2，或把 256/64 偷换成未知 61 点 |
| [RuView MediaTek beta statement](https://github.com/ruvnet/RuView/blob/27f5540663d5eb21753f9d9c0ec6c3f348b710eb/docs/releases/v0.9.1-mediatek-beta.1.md) | audited 版本写明 simulator-first；MTC1 是主机协议，不是物理 MediaTek capture 证明 | 借鉴展示、协议/provenance 与醒目 synthetic 标签 | 不是 AX3000T 实测定位/人体姿态验证来源 |
| [RuView pose benchmark](https://github.com/ruvnet/RuView/blob/27f5540663d5eb21753f9d9c0ec6c3f348b710eb/docs/benchmarks/pose-estimation-cog.md) | 仓库自身区分 synthetic forward pass，报告该 pose cog 的 PCK@20 仅 3.0% | UI 永远显示证据来源与失败边界 | 不把首页动画、模拟吞吐或其他数据集 benchmark 当现场单 AX 骨架精度 |

## 收敛出的实现原则

1. **先证明数据语义。** quality/presence/tone/BW/count/segment 任一硬门失败，
   AoA 与 CIR 都拒绝；`allow-low-confidence` 不能绕过。
2. **先证明同步和 stream。** 强 PPDU 身份之外，还必须同 Tx/transport stream；
   多 receiver 另需共同 timebase 与 clock-uncertainty 门。
3. **校准包含几何方向和独立 holdout。** steering 使用 target-reference 有向
   向量；拟合点与 opposite-side validation capture 分离，cross-angle residual
   失败即拒绝；artifact 绑定 receiver/boot/radio/code/radio-config 并用内容 hash 标识。
4. **双阵元只做 coarse Bartlett。** 输出 13+ sector support、前后镜像和全部
   grating-lobe candidates，不借用大阵列的 MUSIC 精度。
5. **时延只作 relative diagnostics。** 没有跨频拼接或真实 FTM，不输出绝对距离。
6. **距离必须有标签、来源和 capture 绑定。** kNN 输出 support weights；模型
   hash 绑定训练 source IDs。换手机/房间的 deterministic test 必须明显退化并
   降 evidence；未绑定实际 capture/TA/radio/time window 的裸向量禁止参与融合。
7. **展示名称不能制造统计含义。** softmax 叫 normalized support，多点结果叫
   fused support，80% 值叫 display-mass radius；不是 posterior/likelihood/credible interval。
