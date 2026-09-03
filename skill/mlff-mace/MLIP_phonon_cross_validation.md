# 用于声子/力常数（fc2）计算的通用预训练 MLIP 调研笔记

> 调研日期：2025 年（信息核对至各包 2024–2025 最新发布版）
> 场景：掺杂富勒烯（C + 碱土金属 Ba 等）体系，phonopy + symfc / Pheasy 拟合 fc2 出现严重虚频，
> 拟换通用预训练势做交叉验证。
> 结论先行：**优先 MACE-MP-0b3 / MACE-MPA-0、SevenNet-0、CHGNet、MatterSim-v1（参考），
> 力-直出（非能量梯度）模型（ORB v1/v2 直出版、EquiformerV2）做 fc2 需谨慎**。

---

## 0. 三篇关键声子基准（先看结论）

| 论文 | 测了什么 | 关键结论 |
|---|---|---|
| Loew et al., *Universal machine learning interatomic potentials are ready for phonons*, **npj Comput. Mater. 11 (2025)**, arXiv:[2412.16551](https://arxiv.org/abs/2412.16551) | ~9000 个非磁半导体（MDR 库，统一重算到 PBE），谐波声子（有限位移法），测 M3GNet / CHGNet / MACE-MP-0 / SevenNet-0 / MatterSim-v1 / ORB / eqV2-M | **MatterSim-v1 一骑绝尘（误差≈PBE vs PBEsol 差异，近 DFT 精度）**；M3GNet、CHGNet、MACE-MP-0、SevenNet-0 属于"中间组"，系统性**低估频率、高估熵**（训练数据缺少平衡位置小位移构型）；**ORB 与 eqV2-M（力直出、非保守）声子惨败**——最大频率分布严重扭曲、峰在 0 附近，根本原因是"力非能量梯度、PES 曲率（fc2）未定义" |
| Póta et al., *Thermal Conductivity Predictions with Foundation Atomistic Models*, arXiv:[2408.00755](https://arxiv.org/abs/2408.00755) | 103 个 PhononDB-PBE 二元化合物，WTE 热导 + Grüneisen（需要 fc2+fc3） | 5 个模型（M3GNet、CHGNet、MACE-MP-0、SevenNet、ORB-v1-MPtraj）多在半倍~2 倍精度内；**MACE-MP-0 零样本精度最高**；所有 fMLP 系统性低估 κ（低估频率 + 高估非谐线宽） |
| Aghoghovbia et al., *A Comprehensive Assessment and Benchmark Study of Large Atomistic Foundation Models for Phonons*, arXiv:[2509.03401](https://arxiv.org/abs/2509.03401) | 2429 个晶体，fc2/fc3 → 晶格热导 | **EquiformerV2 总体最准（尤其三阶 IFC）**，MatterSim 居中，MACE 与 CHGNet 力精度接近但 IFC 拟合有差异、热导较差；同时指出 EquiformerV2 类模型**PES 平滑性可能有问题** |

**给本课题的启示**
1. 力-直出（force-direct / non-conservative）模型（ORB 直出版、EquiformerV2 系）的 **fc2/Hessian 在原理上不可靠**（力不是能量梯度，曲率无定义）；要保守模型（energy-gradient）。
2. "中间组"普遍**软化**（低估频率）——正是虚频/软模的来源之一：MLIP 训练数据里平衡位置附近小位移构型不足时，fc2 偏软。
3. 自训练 MACE 势的虚频**不能排除是势本身的问题**：单分子富勒烯+金属训练数据可能未覆盖周期晶体里的小位移构型 → 位移-力响应有噪声 → symfc/Pheasy 拟合出的 fc2 带虚频。换通用势交叉验证是正确路线。

---

## 1. MACE-MP-0（及同族 foundation models）— 首推

- **包/安装**：`pip install mace-torch`（PyPI 最新 0.3.16，2025）；权重辅助包 `pip install mace-models`（0.1.6）。文档：https://mace-docs.readthedocs.io
- **ASE 用法**（注意：**mace>=0.3.10 起 `mace_mp()` 默认是 MACE-MPA-0（medium-mpa-0）**，不再是经典 MACE-MP-0）：
  ```python
  from mace.calculators import mace_mp
  # 经典 MACE-MP-0a medium（若想复现旧行为）
  calc = mace_mp(model="medium", default_dtype="float64", device="cuda")
  # 推荐：MACE-MP-0b3（官方注明"修了 b2 的部分声子问题"，MIT 许可）
  calc = mace_mp(model="medium-0b3", default_dtype="float64", device="cuda")
  # 更准：MACE-MPA-0（MPTrj+sAlex，89 元素，MIT）
  calc = mace_mp()                      # == medium-mpa-0
  # 声子口碑最好的 OMAT 版（OMat24 训练，官方标注 "Excellent phonons"，ASL 学术许可，会提示接受条款）
  calc = mace_mp(model="medium-omat-0", default_dtype="float64", device="cuda")
  ```
- **声子适配性**：SO(3) 等变消息传递、能量-梯度保守模型 → Hessian 良定义；平滑；**官方 release notes 明确 MACE-MP-0b3 修复了声子问题、MACE-OMAT-0 标注 "Excellent phonons"**（见 [mace-foundations README](https://github.com/ACEsuit/mace-foundations)）。
- **许可证**：代码 MIT；MACE-MP-0a/0b3/MPA-0 权重 **MIT（可商用）**；MACE-OMAT-0 / MATPES / MH 权重 **ASL（Academic Software License，学术免费，商用需另行授权）**（mace 源码 `foundations_models.py` 中 ASL 检查点集合）。
- **元素覆盖**：89 种（MPTrj / MPTrj+sAlex / OMat24），**含 C、Ba**。
- **声子口碑**：Póta 2025（2408.00755）五模型中零样本热导/声子精度最高；npj 2025（2412.16551）中间组里仅次于 SevenNet-0；MatBench 系基准（WBM/PhononDB κSRME）整体靠前。
- 官方论文：[arXiv:2401.00096](https://arxiv.org/abs/2401.00096)；模型清单：[ACEsuit/mace-foundations](https://github.com/ACEsuit/mace-foundations)

## 2. CHGNet — 第二梯队但可直接用

- **包/安装**：`pip install chgnet`（PyPI 最新 0.4.2，2025；默认预训练权重 0.3.0）。文档：https://chgnet.lbl.gov
- **ASE 用法**：
  ```python
  from chgnet.model.model import CHGNet          # 0.4.x
  from chgnet.model.dynamics import CHGNetCalculator
  chgnet = CHGNet.load()                          # 默认 0.3.0 权重
  calc = CHGNetCalculator(chgnet)
  ```
- **声子适配性**：能量-梯度保守；带电荷/磁矩信息（对磁体系是优点）；平滑性尚可。已知缺点：训练数据（MPtrj 弛豫轨迹）含较大位移，**平衡位置小位移响应相对弱 → fc2 系统性偏软**（npj 2025 中间组）；高频段（如 C-H）误差偏大。
- **许可证**：BSD-3-Clause（Modified BSD）——**可商用**。
- **元素覆盖**：65 种（MPtrj 覆盖），**含 C、Ba、Sr、Ca**。
- **声子口碑**：npj 2025 中间组；2408.00755 里热导精度低于 MACE-MP-0；Matbench Discovery 基准强（稳定性预测），声子非其强项。
- 官方论文：[arXiv:2302.14231](https://arxiv.org/abs/2302.14231)（Nat. Mach. Intell. 2023）；代码 [CederGroupHub/chgnet](https://github.com/CederGroupHub/chgnet)

## 3. SevenNet — 首推梯队（基座就是 NequIP 架构）

- **包/安装**：`pip install sevenn`（PyPI 最新 **0.13.0**，2025；老 API 0.11.x 亦可）。文档：https://sevennet.readthedocs.io
- **ASE 用法**（0.12+ 新导入路径；经典 MP-0 模型名 `SevenNet-0_11July2024`；新版推荐 `7net-omni`）：
  ```python
  from sevenn.calculator import SevenNetCalculator
  calc = SevenNetCalculator(model="7net-omni", device="cuda")     # 0.13 推荐：Omni（mpa 任务默认）
  # 或经典 MP-0：
  # calc = SevenNetCalculator("SevenNet-0_11July2024", device="cuda")
  ```
- **预训练模型**（0.13）：SevenNet-Omni（推荐，多任务，mpa/omat24 等任务）、Omni-i8/i12、Nano、MF-ompa、omat、l3i5、SevenNet-0。文档页自带 **PhononDB κSRME** 指标对比。
- **声子适配性**：SE(3) 等变 GNN（NequIP 基座），能量-梯度保守；平滑；**npj 2025 中间组第一名**（七模型里仅次于 MatterSim-v1）。
- **许可证**：**MIT**（PyPI/GitHub 均确认）——可商用。
- **元素覆盖**：89 种（MP 训练），**含 C、Ba**。
- 官方论文：[arXiv:2403.16019](https://arxiv.org/abs/2403.16019)（JCTC 2024）；[MDIL-SNU/SevenNet](https://github.com/MDIL-SNU/SevenNet)

## 4. M3GNet（matgl）— 备胎/老将，精度垫底但免费

- **包/安装**：`pip install matgl`（PyPI 最新 **4.0.3**，2025）。文档：https://matgl.ai 或 https://docs.materialsproject.org
- **ASE 用法**：
  ```python
  from matgl.ext.ase import M3GNetCalculator
  from matgl.utils.training import load_model
  pot = load_model("M3GNet-MP-2021.2.8-PES")
  calc = M3GNetCalculator(potential=pot)
  ```
- **声子适配性**：能量-梯度保守、平滑；但**精度为第一代，声子误差在基准里明显偏大**（npj 2025 中间组偏后；2408.00755 热导误差大）。
- **许可证**：BSD-3-Clause——可商用。
- **元素覆盖**：89 种，**含 C、Ba**。
- **结论**：可跑但不推荐作为主力交叉验证；仅作低成本粗筛。
- 官方论文：[arXiv:2202.02406](https://arxiv.org/abs/2202.02406)（Nat. Comput. Sci. 2022）；[materialyzeai/matgl](https://github.com/materialyzeai/matgl)

## 5. NEP（GPUMD / calorine）— 需要自训练，但 fc2/fc3 管线最成熟

- **安装**：GPUMD 二进制（GitHub [brucefan1983/GPUMD](https://github.com/brucefan1983/GPUMD)，**GPL-3.0**）+ `pip install calorine`（PyPI 最新 **3.5**，**MPL-2.0**）。文档：https://calorine.materialsmodeling.org
- **ASE 用法**：
  ```python
  from calorine.calculators import CPUNEP
  calc = CPUNEP("nep.txt")          # 需要先训练好的 NEP 势
  ```
- **fc2/fc3 管线**：calorine 内置 `calorine.tools.get_force_constants(structure, supercell_matrix, ...)`（基于 phonopy 有限位移，跑 GPUMD，返回 Phonopy 对象，可带 fc3、非解析项修正）；GPUMD 自带 compute_phonon。**NEP 是无预训练通用模型**：须用随机位移法（mlff-mace 同款思路）自训，训练好之后声子/热导口碑极好（描述子势平滑、能量-梯度保守、速度快）。
- **结论**：不是"拿来即用"的交叉验证选项；如果最终要跑到大超胞/高温 MD 热导，值得作为自训终点。许可证：GPUMD GPL-3.0（商用注意传染性）、calorine MPL-2.0。

## 6. NequIP / Allegro — 无通用预训练，需自训

- **包/安装**：`pip install nequip`（PyPI 最新 **0.19.0**，2025；Allegro 随 nequip 包发布，代码 [mir-group/allegro](https://github.com/mir-group/allegro)）。
- **ASE 用法**：
  ```python
  from nequip.ase import NequIPCalculator
  calc = NequIPCalculator.from_deployed_model(
      model_path="deployed.pth", device="cuda",
      energy_units_to_eV=1.0, length_units_to_A=1.0)
  ```
- **声子适配性**：SE(3) 等变、能量-梯度保守、平滑——自训到位后 fc2 质量好，文献里常用来算声子/热导；**但无 89 元素通用预训练权重（仅有分子体系 ANI 等）** → 不适合快速交叉验证。
- **许可证**：MIT——可商用。
- **结论**：跳过（除非你想自训一个新架构做对照）。

## 7. 其他值得考虑

### 7.1 ORB（orb-models）— ⚠️ 只选 conservative 版
- **包/安装**：`pip install orb-models`（PyPI 最新 **0.7.0**，2025）。文档：https://orbit-ml.readthedocs.io
- **ASE 用法**：
  ```python
  from orb_models.forcefield import pretrained
  calc = pretrained.orbff(device="cuda")           # v2（直出力，非保守，慎用于 fc2）
  # 推荐声子用途：v3 conservative + 无限邻居（避免 20 邻居截断造成 PES 不连续，文档明说影响 Hessian）
  calc = pretrained.orbff_v3_conservative_120_omat(device="cuda", precision="float32-highest")
  ```
- **要点**：v2（2024，arXiv [2410.22570](https://arxiv.org/abs/2410.22570)）与 v1 都是**力-直出**——**npj 2025 明确定位 ORB 声子惨败的根因**；v3（2025，arXiv [2504.06231](https://arxiv.org/abs/2504.06231)）推出 **conservative 版（backprop 求力）+ inf 邻居**，官方用 κSRME（声子+三阶 IFC 派生热导）自证，但独立声子基准仍少。**用它必须选 conservative 模型并检查权重条款**。
- **许可证**：仓库 Apache-2.0（权重随仓库发布，商用条款以当前发布为准）。
- **元素覆盖**：89 种（MP/Alexandria/OMat24 训练），**含 C、Ba**。

### 7.2 GRACE（ICAMS，gracemaker / tensorpotential）
- **安装**：`pip install tensorpotential`（PyPI 0.6.0，2025-2026；社区新版 API 见 "grace-ml" 分支）；基础模型从 HuggingFace [AMS-ICAMS-RUB/grace-foundation-models](https://huggingface.co/AMS-ICAMS-RUB/grace-foundation-models) 下载（GRACE-1L / 2L / 3L-OMAT）。
- **要点**：ACE 树图扩展，89 元素，OMat24 110M 帧训练；**GRACE-2L 在 κSRME（声子派生热导）上拿到最低误差档**（论文自证），平滑、保守。
- **许可证**：**ASL（Academic Software License，学术免费、商用受限）**——与 MACE-OMAT-0 同类，商用前须确认。
- 论文：arXiv:[2508.17936](https://arxiv.org/abs/2508.17936)（npj Comput. Mater. 2026）；[ICAMS/grace-tensorpotential](https://github.com/ICAMS/grace-tensorpotential)

### 7.3 DPA-2 / DPA-3（DeepMD-kit）
- **安装**：`pip install deepmd-kit`（PyPI 最新 **3.2.0**，2025；GPU 版建议 conda）。文档：https://docs.deepmodeling.com/projects/deepmd
- **ASE 用法**：
  ```python
  from deepmd.calculator import DP
  calc = DP(model="DPA-3.2-5M")          # 内置注册表名，自动下载缓存；经典 DPA-2.2 亦可
  # 或先 dp pretrained download DPA-3.2-5M
  ```
- **要点**：DPA-2/3 是能量模型（保守、力=梯度）→ fc2 良定义，这点比 FAIR 的 EquiformerV2 直出版好；但同为 EquiformerV2 基座，2509.03401 提示**其 PES 平滑性可能有问题**（影响有限差分 fc2）；预训练覆盖约 68 种元素（C 覆盖；Ba 需以官方 model card 确认）。声子基准远少于 MACE/SevenNet。
- **许可证**：LGPL-3.0（商用注意传染性）。
- 论文：DPA-2 arXiv:[2312.15492](https://arxiv.org/abs/2312.15492)（npj Comput. Mater. 2025）

### 7.4 MatterSim（微软）— npj 2025 声子第一名（建议加进对照）
- **安装**：`pip install mattersim`（Python ≥3.12；PyPI）。文档：https://microsoft.github.io/mattersim
- **ASE 用法**：
  ```python
  from mattersim.forcefield import MatterSimCalculator
  calc = MatterSimCalculator(device="cuda")           # 默认 MatterSim-v1.0.0-1M；5M 更准
  # calc = MatterSimCalculator(load_path="MatterSim-v1.0.0-5M.pth", device="cuda")
  ```
- **要点**：M3GNet 基座重训 + 主动学习采样（含偏离平衡构型），89 元素，保守、平滑；**npj 2025 谐波声子零样本误差最小（≈PBE vs PBEsol）**，2509.03401 居中偏上。新版本 v1.5/v2 见官方文档。**注意：微软把更完整的版本放到 Azure Quantum Elements（云服务），开源版为 v1 系**——商用条款以仓库为准（代码 MIT）。
- 论文：arXiv:[2405.04967](https://arxiv.org/abs/2405.04967)；[microsoft/mattersim](https://github.com/microsoft/mattersim)

---

## 8. 推荐排序（掺杂富勒烯 C+Ba 声子交叉验证）

| 优先级 | 模型 | 理由 |
|---|---|---|
| ① 首推 | **MACE-MP-0b3 / MACE-MPA-0** | 与现有 MACE 工作流同框架、89 元素含 C/Ba、MIT 可商用、官方修过声子问题；npj 2025 中间组前列、2408.00755 零样本最佳；float64 + 保守梯度 → fc2 可靠 |
| ② 首推 | **SevenNet-0 / 7net-omni** | 基座同 NequIP，平滑等变保守；**npj 2025 七模型里谐波声子仅次于 MatterSim**；MIT；89 元素 |
| ③ 对照 | **CHGNet** | 65 元素含 C/Ba、BSD-3 可商用、零门槛；声子"中间组"，适合做第二对照 |
| ④ 强对照 | **MatterSim-v1（5M）** | npj 2025 声子第一；若接受 Python≥3.12 与微软条款，作为"最高精度参照"最有价值 |
| ⑤ 备选 | **M3GNet** | 快、免费，但声子误差大，仅作 sanity check |
| ⑥ 有条件 | **ORB v3 conservative（inf 邻居）**、**GRACE-2L** | 声子/κSRME 自证优秀，但分别有"必须选 conservative"与 ASL 商用限制；GRACE 需熟悉 gracemaker API |
| 不推荐（此场景） | **NEP/GPUMD、NequIP/Allegro、DPA-2** | 前者需自训（非"拿来即用"）；NequIP/Allegro 无通用预训练；DPA-2 声子基准少且 EquiformerV2 平滑性存疑 |
| 力-直出模型警告 | ORB v1/v2 直出、EquiformerV2(FAIR) | 非保守力 → fc2 无定义，npj 2025 实测声子灾难；不要用于 fc2 拟合 |

## 9. 对当前虚频问题的实操建议（先诊断再换势）

1. **先隔离"势的问题 vs 拟合的问题"**：用同一自训练 MACE 势，跑 **phonopy 冻声子（frozen-phonon，不做 symfc/Pheasy 拟合）** 直接算 fc2；
   - 若冻声子也虚频 → 势本身对周期晶格位移响应有问题（很可能：单分子富勒烯+金属训练数据未覆盖周期晶格小位移构型，超胞外力=外推）；
   - 若冻声子干净而 symfc/Pheasy 虚频 → 拟合环节问题（见下）。
2. **拟合环节排查**（你已有趋势：Pheasy c2 从 6→7 虚频 -1.55→-4.0，说明拟合对正则化极敏感）：
   - 换大位移：MLIP 建议 0.02–0.03 Å（phonopy 默认 0.01 Å 对 MLIP 太小，力噪声→虚频）；DFT 用 0.01 Å；
   - symfc 用 `--asr` + 置换对称（它本身强制 ASR）；检查超胞收敛（2×2×2 → 更大，虚频是否收敛）；
   - Pheasy：减小 c2、增加位移构型数、或改用其 c1+c2 联合正则；
   - 核对 Γ 点声学支：ASR 残差会直接造成 Γ 虚频；用 phonopy 的 `--fc-symmetry` 或 symfc 的对称约束核对。
3. **交叉验证协议**：同一优化结构（用每个势各自再弛豫到力收敛，注意 MACE-MP-0 与 DFT 平衡结构可能不同）→ 同一超胞 → 同一位移 → 各势用 phonopy 冻声子 + symfc 双通道 → 对比虚频大小与高频率段。
4. **判断物理软模 vs 数值伪影**：掺杂富勒烯（如 Ba 插层）本身可能存在真实软模（Ba 振动、笼呼吸、Jahn-Teller/电荷有序导致的结构不稳定）；若各势在**同一 q 点稳定重现同一支虚频**，很可能是真实软模；若虚频大小随位移/超胞/正则化乱跳，则是数值伪影。

## 10. 关键链接汇总

- 基准：npj [2412.16551](https://arxiv.org/abs/2412.16551)；热导 [2408.00755](https://arxiv.org/abs/2408.00755)；全面评估 [2509.03401](https://arxiv.org/abs/2509.03401)
- MACE：[docs](https://mace-docs.readthedocs.io/en/latest/guide/foundation_models.html) / [mace-foundations](https://github.com/ACEsuit/mace-foundations) / 论文 [2401.00096](https://arxiv.org/abs/2401.00096)
- CHGNet：[chgnet.lbl.gov](https://chgnet.lbl.gov) / [CederGroupHub/chgnet](https://github.com/CederGroupHub/chgnet) / 论文 [2302.14231](https://arxiv.org/abs/2302.14231)
- SevenNet：[docs](https://sevennet.readthedocs.io/en/latest/) / [MDIL-SNU/SevenNet](https://github.com/MDIL-SNU/SevenNet)
- M3GNet：[materialyzeai/matgl](https://github.com/materialyzeai/matgl) / 论文 [2202.02406](https://arxiv.org/abs/2202.02406)
- NEP/GPUMD：[GPUMD](https://github.com/brucefan1983/GPUMD) / [calorine](https://calorine.materialsmodeling.org)
- NequIP/Allegro：[mir-group/nequip](https://github.com/mir-group/nequip) / [allegro](https://github.com/mir-group/allegro)
- ORB：[orb-models](https://github.com/orbital-materials/orb-models) / v3 论文 [2504.06231](https://arxiv.org/abs/2504.06231)
- GRACE：[ICAMS/grace-tensorpotential](https://github.com/ICAMS/grace-tensorpotential) / 论文 [2508.17936](https://arxiv.org/abs/2508.17936)
- DPA-2：[deepmd-kit](https://github.com/deepmodeling/deepmd-kit) / 论文 [2312.15492](https://arxiv.org/abs/2312.15492)
- MatterSim：[microsoft/mattersim](https://github.com/microsoft/mattersim) / 论文 [2405.04967](https://arxiv.org/abs/2405.04967)
