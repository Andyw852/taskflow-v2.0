# mlff-mace —— 随机位移法 MLFF 训练（MACE 势函数产出流水线）

> 技能名 `mlff-mace`；`desc: 随机位移法MLFF训练`。
> 复现 **autoplex phonon workflow**（J. Chem. Phys. **153**, 044104 (2020)；Nat. Commun.
> **16**, 7666 (2025)）的物理配方与验收标准，用 taskflow 的步骤机制重新实现。
> 我们不安装 autoplex（它绑定 atomate2 + jobflow + MongoDB，与 tf 是两套调度器），
> 只照抄其物理配方与验收标准。

**只做一件事：产出一个经过验证的 MACE 势函数权重文件。**
交付物：`<MACE_MODEL_DIR>/<材料>_ft.model`、`model_card.json`、`results_<材料>.txt`、
`convergence_history.json`、以及一行可直接填进 `kl-mace-cpu`/`phonon-mace-cpu` 的
`MACE_MODEL` 值。**不做** κ 生产计算、fc3 生产拟合、生产 MD。

核心论断（本技能要复现的东西）：
> 用 phonopy 单原子位移超胞 + 同一原胞生成的一组随机位移（rattle）超胞，
> 就能建出让 MLIP 复现准确声子结构的晶体数据库。**全程不需要分子动力学。**

---

## 0. 九步流水线

| seq | 步骤 | label | 在哪跑 | 干什么 |
|---|---|---|---|---|
| 1 | `step1_relax` | S1_relax | 计算节点（VASP，12 核） | 原胞紧弛豫：三段式 a/b/c，`EDIFFG=-0.001`；判据 = 收敛 + 末次 `external pressure` ≤ 2 kB（2D 只看面内）。输出带隙（定 ISMEAR）与磁矩（定 ISPIN/MAGMOM）的 EIGENVAL/OUTCAR |
| 2 | `step2_supercell` | S2_cell | 登录节点 run:gen | 判维度；从基座 `.model` 读 `r_max`（不许写死）；按 §5.1 定超胞；校验基座模型覆盖体系全部元素 |
| 3 | `step3_calib` | S3_calib | 登录节点 run:gen | 基座模型 CALIB_FC2 → u_rms(300K) → `RATTLE_STD=[0.5,1.0,1.6]×u_rms` |
| 4 | `step4_genstruct` | S4_gen | 登录节点 run:gen | 停机守卫；生成本代全部待标注构型（rattle 网格 + 孤立原子 + 单原子位移集 + static EOS 帧），写 `struct_manifest.json` |
| 5 | `step5_label` | S5_label | 计算节点，**fanout** `cfg-*`（12 核/帧） | DFT 单点。**唯一昂贵的一步** |
| 6 | `step6_dataset` | S6_data | 登录节点 run:gen | OUTCAR → extxyz；指纹校验；extend 并入；离群过滤；FPS 排序；固定测试集；e0s.json |
| 7 | `step7_finetune` | S7_ft | GPU/CPU，**fanout** `seed-*` | MACE **naive 单头**微调（MULTIHEAD=false，单材料专用势不需要多头），N_COMMITTEE=4 个 seed，全量数据 |
| 8 | `step8_benchmark` | S8_bench | GPU/CPU（sbatch） | §9.2 全部验收闸 + 学习曲线 + 决策表 + 停机判定 + 5 张图 + `results_<材料>.txt` |
| 9 | `step9_publish` | S9_pub | 登录节点 run:gen | 仅当 `status=="pass"` 才拷 `.model` 进 MACE_MODEL_DIR + 写 `model_card.json` |

### 第一次怎么跑（3D 体相，以 Si 为例）

```bash
cd <项目根>                       # 含材料目录（材料目录下有 POSCAR）
tf -tt mlff-mace init             # 建项目设置（自动把技能模板拷进 project_setting）
tf -tt mlff-mace -p Si start      # 一键推进（会先 gen S1 再 sbatch）
# S1 算完后：
tf -tt mlff-mace -p Si start      # 继续推进 S2→S3→S4→S5（S5 一次交 ~25 个 12 核作业）
tf -tt mlff-mace -p Si start      # S5 全部 OK 后继续 S6→S7→S8
tf -tt mlff-mace -p Si start      # S8 pass 后 S9 发布
```

### 第一次怎么跑（2D 材料）

```bash
# 前提：POSCAR 真空沿 c 轴、真空 ≥ max(MIN_VACUUM, 2·r_max) ≈ 15 Å
tf -tt mlff-mace init
tf -tt mlff-mace -p MoS2 conf --set params.FUNC=pbe-d3   # 需要色散修正的 2D 层状材料
tf -tt mlff-mace -p MoS2 start
# 2D 自动生效：超胞真空方向恒 1；应变只做面内；STRESS_WEIGHT=0/ENERGY_WEIGHT=10；
# 验收多一条 ZA 支闸（§9.2 #2b）；模型卡里标注「未考虑 LO-TO 劈裂」。
```

### 手动推进下一代（代数迭代）

taskflow 是 DAG，代数用 `step.conf` 的 `GENERATION` 表达：

```bash
tf -tt mlff-mace -p Si conf --set params.GENERATION=1
tf -tt mlff-mace -p Si -j 4 retry    # ★ 用 retry 别用 rerun：rerun 会删掉 gen-0..gen-(K-1) 历史清单+结构，
#                                  S6 累计数据集会丢帧；retry 保留它们并重生成新代清单
tf -tt mlff-mace -p Si start         # 判据检测到「代数不一致」→ 5/6/7/8 自动补生成/重跑/提交
```

`GENERATION > MAX_GENERATION` 时 gen 直接 `sys.exit`；停机（`halt_*`）后 gen 拒绝推进，
除非显式 `FORCE_CONTINUE=true`（§7.4）。

### ★ step5_label 操作定则（必须原文遵守）

> `step5_label` 是扇出步骤，且是整条链唯一花大钱的地方。
> **只用 `retry` 或 `start -f`，绝不用 `rerun` 和 `clean`** —— 后两者会 `rm -rf` 步骤目录，
> 毁掉已经算完的 DFT 帧。`retry` 会先 `scancel` 在跑的作业再重跑 gen（gen 幂等，
> 不删文件、不碰已算完的 `cfg-*`）；单独补某几帧就进对应 `cfg-*` 目录手工 `sbatch`。

## 0.1 本技能在 Si 上验证后的关键经验（换材料必读）

> 下面三条是从 B0 偏 29% 一路排查到 0.073 THz 声子谱的**实测结论**，不是理论推断。
> 每条都直接决定你换材料时第一步怎么配。

**① 基座选 OMat24 系，不是 MP-0。** MP-0（MACE-MP-0）在 Si 上未微调 B0=76.5
（DFT 95.1，偏软 20%）——它的训练数据全是几何优化过的结构，系统性**低估**能量/力/声子
（已知 softening）。换 `MACE-matpes-pbe-omat-ft`（OMat24 系）后未微调 B0=105，微调后 96.2。
换基座只改 `MACE_MODEL` 一个键，零代码改动。先试 MACE 系更强基座（matpes / MACE-MPA-0 /
MACE-omat），再谈换架构。

**② 单材料专用势用 naive 单头，不用多头 replay。** 本技能产物是交给下游
`kl-mace-cpu`/`phonon-mace-cpu` 算**单一体系** κ/声子的专用势，不需要跨体系泛化。
多头 replay 的两个坑：`num_samples_pt=30000` 对百帧量级的训练集是 ~200:1 **反向淹没**
（pt_head 主导梯度、Default head 饿着，Si 实测 B0 +21.5%）；且 replay 采样**没固定随机种子**，
同 seed 同配置两次训练 B0 能差 20 GPa（§9.2 ①）。naive 单头（MULTIHEAD=false）是窄目标
应用的第一强基线，且确定可复现。

**③ 应力必须真的进损失函数。** 历史上 `--loss` 传两次/硬编码 `--loss=weighted` 覆盖用户配置，
导致应力项从未进损失，B0 偏 29% 却无人发现。修掉之后还要注意权重：`STRESS_WEIGHT_3D` 默认
1.0 时应力被力权重 100 淹没，B0 仍偏（Si 实测 sw=10 → B0=123）；提到与力平权（sw=100）后
B0 收敛（→90.9）。**检查：训练命令里 `--stress_weight` 是不是真的在，且没被力权重压一个数量级。**

**验证过的边界（别把 Si 的成功当普适）：**

- **只在 Si 上完整跑通过一次**——单元素、高对称、基座样本最密，是最容易的情况。
- **所有绝对阈值都是在 Si 上标定的**（§9.1 ① 的推广）：Si 在 ±6% 应变下 E(V) 曲率信号
  约 20 meV/atom；换个软材料（体模量 20 GPa 量级）同样应变下信号只有 ~4 meV/atom，
  "能量 RMSE < 3 meV/atom" 立刻变成空闸。**换体系第一件事是重算被测信号量级再定阈值。**
- **2D 分支（应力权重归零、EOS 换 E(面积)、ZA 支闸）一次都没真实跑过**——代码写了没验。
- **数据集偏小的后遗症还在**：验证集 ~12 帧让 `lowest_loss` 靠噪声刷新（seed4 的 1192），
  测试集 14 帧让 #9 分辨率不足（§9.2）。同一个根：数据集太小，多个基于它的判据在测噪声。

---
## 1. 出处与「与 autoplex 默认值的差异清单」

物理配方与验收标准照抄 autoplex（数据生成 `data/phonons/`、拟合 `fit/`、基准
`benchmark/phonons/`）。以下是我们**有意为之**的差异：

| # | autoplex 默认 | 本技能 | 理由 / 代价 |
|---|---|---|---|
| 1 | 基准声子用 `min_length=20` 的大胞 | **基准超胞 = 训练超胞**（满足 2·r_max 的最小超胞），声子谱只在 commensurate q 点上比较 | 不单独烧一套大胞 DFT。代价：q 点分辨率较粗，长波行为检验能力弱一些 |
| 2 | DFT 参考位移 0.01 Å（提示词 §3.4 的省钱方案） | **0.1 Å = autoplex 默认**。实测 0.01 Å 的位移力只有 ~0.09 eV/Å，在力损失里被 rattle 帧淹没 ~1000 倍——微调后模型光学支软 27%、声子 RMSE 2.8 THz；0.1 Å 把谐波区权重提 100 倍（源码的 0.1 不只是"双倍成本"，它也是损失配平的物理参数） | 以源码为准；标定 CALIB_FC2 仍用 0.01（MACE 免费、保持谐波干净） |
| 3 | 第 0 代 rattle 帧数较大 | **默认 3 应变 × 3 幅度 × 2 种子 = 18 帧起步**，不够再定向加 | 计算量第一优先级（§2） |
| 4 | 应变含义 | 3D 按**体积因子**等比缩放（晶格 × f^(1/3)）；2D 面内晶格因子 f | 照提示词 §4.1/§5.2 |
| 5 | 离群过滤后直接丢弃 | 被过滤帧**不删除**，extxyz 里标 `filtered: true` 保留复查 | 可审计 |
| 6 | `FORCE_LIMIT` 0.1 eV/Å（提示词 §8.3 数值） | **40.0 eV/Å** = autoplex `data_distillation` 的 `force_max` 默认值（源码 `fitting/common/flows.py`）。0.1 会把大幅度 rattle 帧全部滤掉（提示词自己也警告了这一点）；凡冲突以源码为准 | 力分量上限语义与 autoplex 一致 |
| 7 | 训练/测试按轮次随机划分 | 测试集按 **cfg id 哈希固定**（≈10%），跨代永不重分 | 学习曲线与逐代 RMSE 可比 |
| 8 | EOS/体模量独立流程 | 复用 static 帧（VOL_FACTORS 各档的未位移超胞）+ MACE 细网格，两边同法 BM 拟合（B0'=4） | 不额外烧 DFT |
| 9 | Grüneisen 用 ±1% 应变 fc2（两边同法） | 同左；displ 帧在 ±GRUNEISEN_STRAIN 各生成一套（一物两用，也是训练数据） | 验收唯一的三阶敏感量 |
| 10 | INCAR 由静态模板统一生成 | `gen_step5_label.py` 按 step1 输出**自动推导**（带隙→ISMEAR、磁矩→ISPIN/MAGMOM），`[incar.final]` 可覆盖 | 体系无关 |
| 11 | 能量离群过滤（提示词 ENERGY_LIMIT 相对组中位数） | 保留，但**组员 < 4 时跳过**——2 个种子的组定不出中位数，硬套会误杀 | 组 = config_type + 应变 + 幅度档 |
| 12 | 微调 `--E0s=e0s.json`（提示词 §10） | **默认 `--E0s=estimated`**（`E0S_MODE=estimated`，可切 `json`）。基座 E0（如 MACE-MP-0 的 Si ≈ −7.7 eV）与目标泛函零点差 ~8 eV/atom，直接把 DFT 孤立原子能量塞进新头会让训练从 8 eV/atom 的初始能量误差起步、ENERGY_WEIGHT=1 下 30 代纹丝不动（实测）；`estimated` 是 mace 官方给微调做的零点对齐（用基座对训练数据的预测回归 E0s）。孤立原子帧照算（训练数据 + model_card 备查） | 以源码/工程实测为准 |
| 13 | 微调 30 epochs / lr=1e-4 / batch 4 / loss=weighted / stress 3D=10（提示词 §10） | **autoplex `_mace_hypers.py` 默认：lr=1e-3、max_num_epochs=1500、patience 早停、swa+start_swa=1200、loss=huber、batch=10、stress_weight=1.0**。multihead 模式 mace 会把 lr 摁回 1e-4，必须 `--force_mh_ft_lr=true`。实测 30 ep/lr 1e-4 下力基本没离开基座（光学支软 27%、声子 RMSE 2.8 THz） | 以源码为准 |

---

## 2. 计算量控制（本技能的第一优先级）

DFT 单点是整条链唯一昂贵的东西；其余（结构生成、MACE 微调、验收、学习曲线）在
GPU/登录节点上都是分钟级。原则：**GPU 时间随便花，DFT 帧数掐死。**

1. **第 0 代起步就小**：默认 18 帧 rattle，不够再定向加，绝不一上来撒 60 帧。
2. **单原子位移集一物两用**：既是训练数据，又是 DFT 声子基准来源。不为验收单独再算 DFT。
3. **基准超胞 = 训练超胞**（差异清单 #1）。
4. **DFT 参考位移只算一个幅度**（差异清单 #2）。
5. **后续代关掉声子数据生成**：第 0 代生成 displ/iso，之后各代只追加 rattle。
6. **扩算是定向的**：第 K 代新增帧由 `step8` 的 `plan.json` 指定投向哪个（应变, 幅度）格点（§7.3）。
7. **学习曲线不花任何 DFT**：在已标注帧上做嵌套子集重训（§7.2）。
8. **孤立原子只在第 0 代算一次**，`ISO_BOX=15 Å`、Γ-only。
9. **`DATA_MODE=extend` 能跳过就跳过**：给 `REF_FC2_PATH` 就不算 DFT 声子基准；给
   `PRE_XYZ_FILES` 就先做覆盖分析，新帧只投到盲区（§4）。
10. **停机规则硬性生效**（§7.4）。

`step8` 日志打印一行累计成本：
`[COST] gen=K 累计 DFT 单点 N 帧（rattle A + 位移 B + 孤立原子 C），本代新增 M 帧`。

典型第 0 代 DFT 帧数（scratch，3D）：
`rattle 18 + displ(0/±1%) 3×N_sym + static 3 + iso 1`。
Si（4×4×4=128 原子，高对称）N_sym=1 → **25 帧 × 12 核**。
低对称大胞 N_sym 可能到几十：超过 `DISP_WARN=80` 时 WARN 并提示提供外部 `REF_FC2_PATH`
或减小超胞（§3.2）。

---

## 3. 数据配方

### 3.1 随机位移（rattle）超胞 —— 主力数据

**为什么它是主力**：一个 N 原子 rattle 超胞里所有原子都被位移，提供 3N 个相互独立的
力分量，且每个原子处在互不相同的局域环境；单原子位移超胞中只有一个原子偏离理想位置，
其余 N−1 个原子的环境几乎都是完美晶体——那个环境数据库里已经有了，重复采它不带来新信息。

生成 = **应变网格 × 位移幅度网格 × 随机种子**（hiphive MC-rattle，与 autoplex 同款引擎；
`d_min = MIN_DIST_RATIO × 共价半径和` 保护，拒绝重抽样 ≤ 50 次，拒绝次数记进 manifest）：
- 应变：3D → 等比体积缩放 `VOL_FACTORS`（默认 0.97/1.00/1.03）；2D → 面内双轴应变
- 位移：`RATTLE_STD` 三档 = `[0.5, 1.0, 1.6] × u_rms(300K)`（step3 用基座模型自校准）
- 种子：`N_PER_CELL`（默认 2）→ 第 0 代 3×3×2 = 18 帧

幅度档自校准为什么用基座模型：定幅度只需要量级（差 20% 无所谓），基座模型免费。
基座模型有虚频导致 u_rms 算不出 → 退化 `RATTLE_STD_FALLBACK` 并 WARN，提示最终模型的
软模行为要重点看 §9.2 #2/#2b。自校准值与 fallback 两组都写进 `calib_summary.json`。

### 3.2 phonopy 单原子位移超胞 —— DFT 声子基准 + 训练数据

数量由对称性决定；位移幅度 `REF_DISP=0.01 Å`。**只在第 0 代且没给 `REF_FC2_PATH` 时计算**，
在 0 / ±GRUNEISEN_STRAIN 三个应变下各生成一套（±1% 应变那两套供 Grüneisen 验收，§9.2 #10）。
`step4` 日志预先报出帧数，> `DISP_WARN=80` 就 WARN 并提示提供外部 `REF_FC2_PATH` 或减小超胞。

### 3.3 孤立原子（E0s）

每元素一个，`ISO_BOX=15 Å` 立方盒、Γ-only、同一套 DFT 设置。只在第 0 代算。
**磁性元素必须 `ISPIN=2` 并给合理初始 MAGMOM**（晶体中该元素平均磁矩 |m|>0.1 用它，
否则用高自旋起点表），否则原子参考能是错的、数据集能量零点跟着错。收敛困难时降 ALGO、
加 NELM；连续三次不收敛就报错退出（绝不拿没收敛的能量当 E0s）。

### 3.4 static 帧（EOS 基准）

VOL_FACTORS 各档的未位移超胞（3 个）——既是 EOS 验收的 DFT 基准点（§9.2 #8），
也是训练数据与 REF_FC2 的残余力参考。

---

## 4. DATA_MODE：scratch / extend

```ini
DATA_MODE     = scratch        # scratch | extend
PRE_XYZ_FILES =                # extend：逗号分隔的 extxyz 路径
REF_FC2_PATH  =                # 外部 DFT fc2（给则跳过 displ 帧的 DFT 计算）
```

- **scratch（默认）**：不需要任何外部输入，第 0 代自己算 18 帧 rattle + displ + iso + static。
- **extend**：`step4`（gen0）先对 `PRE_XYZ_FILES` 做**覆盖分析**（体积/面积分布、RMS 位移
  直方图、元素力统计 → `coverage_report.json` + `coverage_plan.json`），把新采样压到盲区：
  已有帧体积集中在一个值附近（典型：固定胞 rattle）→ 1.00 档种子降到 1、其余档加钱，
  决定与理由打进日志（§4.3）。`step6` 并入老数据前做**指纹校验**（extxyz 里的
  `mlff_fingerprint`）：不一致整批丢弃 + WARN，不阻断；没有指纹字段的老数据默认接受并
  WARN「用户负责确认一致性」。

---

## 5. 维度处理：3D 与 2D

维度用 `dim_common` 自动判定（真空阈值 8 Å），也可 `DIMENSION=2d|3d` 强制。2D 不是
「3D 流程把真空方向倍数锁 1」就完事：

- **超胞**：真空方向倍数恒 1；`≥2·r_max` 判据只对面内生效；真空厚度 <
  max(MIN_VACUUM, 2·r_max) 直接报错提示先加真空。
- **应变**：面内双轴 `a→f·a, b→f·b`，真空方向长度保持不变（gen 脚本断言，不等就
  `sys.exit`）——等比缩放三个格矢会压缩真空层，让模型学到「真空厚度影响能量」的假关联。
- **应力**：面外分量（zz/xz/yz）无物理意义，**2D 一律 `--stress_weight 0`**，extxyz 里写
  `config_stress_weight = 0.0`（MACE 官方对含真空体系的处理）。代价是失去应变的直接监督，
  补偿：2D 时 `ENERGY_WEIGHT` 从 1.0 → **10.0**，EOS 闸换成能量–面积曲线 + 面内二维模量。
- **ZA 支**（2D 最容易翻车）：面外声学支在 Γ 附近二次色散，对残余力、ASR 违反、旋转求和
  规则极其敏感，MLIP 出假虚频是常见现象。验收单设一条闸（§9.2 #2b：
  |q|<0.05|b| 内最低频率 ≥ −0.05 THz，比 3D 的 −0.1 严）；`<材料>_rmse_phonons.png`
  把 ZA 支单独标注；排查顺序：① 是否用微调后的势自身重弛豫过（残余力）② ASR 是否满足
  ③ 面内超胞是否够大 ④ 最后才怀疑材料本身。
- **NAC**：2D 的 LO-TO 非解析项形式与 3D 不同，phonopy 的常规 3D NAC 用在 2D 上是错的，
  MACE 本身也给不出 Born 有效电荷。**2D 默认不加 NAC**，model_card 与验收报告标注
  「未考虑 LO-TO 劈裂」。
- **rattle 与平板漂移**：2D 的 rattle 仍是各向同性三维位移（面外位移是物理的，正是 ZA 模式），
  但检查质心沿真空方向漂移 < 0.5 Å 且原子不进入周期镜像真空区，超了重抽样。
- **2D 弛豫（S1）必须锁真空方向，且必须用 optcell 版 VASP**：三段式弛豫的 b/c 段是
  `ISIF=3`（放开胞），2D 必须用 `IOPTCELL` 锁死真空方向（c 轴）及含 c 的剪切，否则真空层
  会被压扁——qHPC36（C36 笼）实测 c 轴 25→8.2 Å、能量爆炸。**这是两重坑**：① `IOPTCELL`
  是 optcell 补丁版的标签，**标准 vasp.6.6.0 不认识它**，写了也无效。3090 的
  `~/software/vasp_optcell/` 就是 **vasp 6.4.3 + optcell 补丁**（version.F 确认 6.4.3），
  CPU 版（openmx_build 环境 + `--partition=cpu192`，gcc/gfortran+MPI 编译，非 GPU）；
  jzzn 的 `vasp.6.4.3-optcell` 同理。Ba1C20 验证：b 段 ISIF=3 + IOPTCELL 下
  c 轴保持 25 Å 未压扁（标准版 25→8.2 发散）。② 模板注释说
  「IOPTCELL 由 relax_common 注入」，而 relax_common 的 `CELL_CONSTRAINT_2D='ioptcell_tag'`
  模式是「原样保留」——两边各自以为对方在管，结果没人写（与 `--loss` 被覆盖、
  `PATIENCE` 不跳出循环、required 闸不影响 status 同一类）。**验收侧（mace_relax.py 的
  FrechetCellFilter mask）和应变侧（rattle_gen.py 的真空断言）都正确处理了 2D，唯独 S1
  模板漏了**——这正说明「真空方向没锁」在 2D 路径上会重复出现，逐个位置查。

---

## 6. DFT 设置（step5_label，§8.1 基线）

```
PREC=Accurate；ENCUT=ceil(1.5×max ENMAX)（ENCUT_OVERRIDE 可覆盖）
GGA/IVDW=由 FUNC 决定（pbe/pbesol/pbe-d3）；与 step1 完全一致
ISMEAR/SIGMA：step1 EIGENVAL 带隙 > 0.1 eV → 0/0.05；否则 1/0.2
ISPIN/MAGMOM：step1 末次磁矩 max|m| > 0.1 μB → ISPIN=2 + 逐原子继承（超胞按
            image-major 展开；磁性体系绝不许用 VASP 默认初值——会跑到不同磁态，
            同一几何得到不同能量，数据集直接被污染）
LASPH=.TRUE.；LMAXMIX：含 d 元素 4 / f 元素 6 / 否则 2；LREAL=.FALSE.
EDIFF=1E-7；NELM=200；ALGO=Normal；ISYM=0；IBRION=-1；NSW=0
LWAVE/LCHARG=.FALSE.；【不许出现 MAXMIX】
KPAR：从 IBZKPT 推（Γ-only 强制 1；12 核小作业 KPAR=1）；NCORE 默认 4
DFT+U：沿用 step1 的 LDAU 设置（relax_common 阴离子门控）；U_OVERRIDE 生效时必须
       在所有构型上一致并计入指纹
KPOINTS：默认 Γ-only（超胞取力标准做法），KPOINTS_GRID 可显式覆盖
```

- `EDIFF=1E-7` + `LREAL=.FALSE.`：单点算力必须比常规静态更严（autoplex 的
  TightDFTStaticMaker 同一用意）。这两项只增加电子步，成本可接受。
- `ALGO=Normal` 而非 Fast：Fast 在大位移构型上偶发不收敛。
- 磁性体系在 model_card 标注「MACE 不含自旋自由度，本模型只对训练时的磁序有效」。

### 指纹校验 `dft_fingerprint()`

`ENCUT/PREC/GGA/IVDW/ISMEAR/SIGMA/ISPIN/LREAL/LASPH/LMAXMIX/EDIFF/LDAU*/POTCAR TITEL`
拼成指纹字符串，写进每步 `*_summary.json` 与每个 xyz 帧（`mlff_fingerprint`）。
**k 点特殊处理**：不同超胞网格必然不同，比等效 k 点密度
`KSPACING = 2π / min_i(N_i·|b_i|)`，容差 `KSPACING_TOL=0.20`——超差 WARN 不阻断；
其余键不一致 `sys.exit` 并指出哪个键、两边各是什么值。

---

## 7. 验收曲线与收敛/停机规则（控制核心）

### 7.1 逐代循环

第 K 代在 `step4_genstruct/gen-<K>/`（结构）、`step6_dataset/gen-<K>/`（数据）、
`step8_benchmark/gen-<K>/`（验收）下工作，**只增不清**，gen 脚本幂等。
训练集与测试集**跨代累积，永不丢弃**（autoplex 非晶砷那篇工作的做法）。

每代结束 `step8` 往 `convergence_history.json` 追加：

```json
{"gen": 1, "n_frames": 45, "rmse_thz": 0.31, "force_rmse_meV_A": 42.1,
 "delta_rmse": -0.09, "curve_plateau": false, "gates_failed": ["#1 声子谱 RMSE"],
 "status": "expand", "reason": "曲线未平且主闸未过，数据量不足，定向加采"}
```

### 7.2 学习曲线（不花任何 DFT）

对全部已标注帧跑一次贪心 FPS 得到有序列表——FPS 是贪心增量的，**前缀天然嵌套**
（S25 ⊂ S50 ⊂ S100 ⊂ …），取前 `CURVE_POINTS=25,50,100,200,all` 个子集**不需要重采样、
不丢弃任何一帧**。要点：
- 测试集在所有曲线点上**同一个**（id 哈希固定），永不进任何训练子集；
- 每个点**独立从基座模型开始**微调（同一 seed、同一套超参），**不许 warm-start** 上一个点；
- 记两条曲线：test 力 RMSE (meV/Å) 与声子频率 MAE (THz)——它们经常**不同步收敛**，
  声子 MAE 才是本技能关心的量；
- **判平**：从 N 到 2N，两条指标相对改善均 < `CURVE_TOL` → `curve_plateau=true`；
- 最终生产模型永远在全量数据上训练；曲线里的子集模型是一次性的，扔的是 GPU 时间。

### 7.3 每代决策表（step8 输出 status）

| 主闸（声子 RMSE < RMS_MAX） | 学习曲线 | status | 动作 |
|---|---|---|---|
| 过 | 任意 | `pass` | 进入 step9 发布 |
| 未过 | 未平 | `expand` | 数据量不足 → 定向加采 GEN_INCREMENT 帧 |
| 未过 | 已平 | `halt_not_data_limited` | 加数据没用 → 停机 + 六条排查清单 |

`halt_not_data_limited` 排查清单：① 位移幅度窗口是否太窄 ② 训练超胞是否太小
③ DFT 设置是否与验收基准一致 ④ 基座模型是否适配该体系（元素覆盖、CALIB_FC2 虚频）
⑤ 离群过滤是否把有效数据滤掉（FORCE_LIMIT 过严）⑥ 微调超参。

`expand` 的加采**必须定向**，理由写进 `gen-<K>/plan.json`（不许黑箱）：
① q 点逐点 RMSE 图误差集中 → 加大对应幅度档采样；② committee σ_F 外推率超阈值 →
在 σ_F 最大构型所在（应变, 幅度）格点加采；③ 都不明显 → 加密应变网格（±0.06/±0.09）。

### 7.4 停机规则（硬性生效）

- 第 K 代改善 `Δ_K = rmse(K−1) − rmse(K)`；`Δ_K < IMPROVE_MIN` 且主闸未过 → 本代记为
  `stagnant`。**连续两代 stagnant** → step8 判 FAIL、`status="halt_stagnant"`。
- `step4` 下一次被调用时检测到 `convergence_history.json` 的 `halt_*` 状态直接
  `sys.exit`，除非显式 `FORCE_CONTINUE=true`。`GENERATION > MAX_GENERATION` 同样硬停。
- 无论哪种停机，**保留全部已有数据和最后一代模型**，`model_card.json` 如实写
  `"converged": false` 及未通过的闸。**绝不允许把没收敛的模型当成通过发布。**

---

## 8. MACE 微调（step7，死规则）

**默认 naive 单头微调（MULTIHEAD=false）**——单材料专用势不需要跨体系泛化，
多头 replay 对百帧级训练集是反向淹没（§0.1 ②）。命令（naive 版）：

```bash
python mace_finetune.py --name=<材料>_gen<K>_seed<S> --foundation_model=<基座.model> \
  --multiheads-finetuning false --num-samples-pt 30000 \
  --train-file=train.xyz --valid-fraction 0.10 --test-file=test.xyz \
  --energy-key=REF_energy --forces-key=REF_forces --stress-key=REF_stress \
  --energy-weight=… --forces-weight=100 --stress-weight=… \
  --lr=0.001 --epochs=1200 --batch-size=10 --huber-delta 0.1 \
  --scaling=rms_forces_scaling --dtype=float64 --device=… --seed=S
```

1. **`DTYPE=float64` 不许改**：float32 力误差 ~1e-3 eV/Å，足以在声学支造出几十 cm⁻¹ 的假虚频。
2. **应力权重按维度自动设，且 3D 必须 ≥ 力权重量级**：3D → `STRESS_WEIGHT_3D`（默认 1.0，
   **Si 实测要提到 100 与力平权**，否则应力被力权重 100 淹没、B0 学不到，§0.1 ③）/ENERGY=1；
   2D → STRESS=0/ENERGY=10（§5）。
3. **`MULTIHEAD`（默认 false=naive）**：单材料专用势用 naive。多头只留给需要分布外鲁棒性的
   场合（如长时 MD 探索未知构型）；开多头必须按 10:1 **偏向目标数据**设 `NUM_SAMPLES_PT`
   （百帧级 ~1500，别用 30000），且先做同 seed 复现检查（§9.2 ①，replay 采样无种子）。
4. **replay 只在多头模式必需**（MULTIHEAD=true 时）：集群无外网，`REPLAY_XYZ` 指向本地文件，
   缺失时报可行动诊断。naive 单头不读 replay。
5. **`PATIENCE` 单卡训练不生效（上游 bug）**：mace-torch 0.3.16 非分布式时 `exit_now=None`，
   早停只打日志不跳出循环，一定训到 `EPOCHS`。模型取的是**最佳 checkpoint**（验证损失改善才
   保存），结果有效，但白烧 GPU。`EPOCHS` 按「真实收敛点 × 1.5」设（§9.2 ⑥）。
6. `BATCH_SIZE` 默认 10，GPU OOM 自动降 1 并记日志。
7. `N_COMMITTEE=4`，seed 固定 1,2,3,4（可复现）。
8. **GPU 分卡（`N_GPU`）**：gen 按 seed s → `CUDA_VISIBLE_DEVICES=(s-1) % N_GPU` 均摊；
   `N_GPU=0`（auto）= N_COMMITTEE 张卡，>0 显式卡数。**提交前看 `nvidia-smi`**：卡被别的
   作业占满时该 seed 直接 OOM（Si 实测 GPU 0/1 被 VASP 占导致 seed1/2 OOM）。
9. **`E0S_MODE` 默认 estimated**（mace 官方为微调做的零点对齐）。单元素体系影响小；
   多元素体系换 `json`（用 DFT 孤立原子能）前先确认基座 E0 与目标泛函零点差不会让训练
   从巨大初始误差起步。

---

---

## 9. 验收闸（step8，写进 `validation_summary.json`，每条 {value, threshold, pass}）

DFT 参考 = REF_FC2（DFT displ 帧力拟出，或外部 `REF_FC2_PATH`）；MACE fc2 = 用微调后
模型在**同一超胞、同一 q 网格**上算。

| # | 指标 | 阈值 | required |
|---|---|---|---|
| 1 | **声子谱 RMSE vs DFT fc2**（commensurate q 网格） | < RMS_MAX = 0.2 THz | ✅ |
| 2 | **imagmodes(pot) == imagmodes(dft)**（3D 阈值 −0.1 THz） | 布尔相等 | ✅ |
| 2b | （仅 2D）ZA 支：|q|<0.05·|b| 内最低频率 | ≥ −0.05 THz | ✅(2D) |
| 3 | **平衡结构残余力**（微调模型 + FrechetCellFilter + FixSymmetry 自弛豫后） | max‖F‖ < 1e-3 eV/Å，空间群不变 | ✅ |
| 4 | **ASR 违反量**（MACE 原始 fc2 声学求和残差） | < 1e-3 eV/Å² | ✅ |
| 5 | 测试集力 RMSE（相对判据：RMSE / 力模长 RMS） | < 3%（`--force-rel-tol`；绝对 40 meV/Å 对 rattle 大位移帧无判别力） | ✅ |
| 6 | 测试集能量 RMSE | < 3 meV/atom | ✅ |
| 7 | 弛豫晶格常数 vs DFT | 各方向偏差 < 1%（2D 只看面内） | ✅ |
| 8 | EOS：3D E(V)+体模量；2D E(面积)+面内二维模量（两边同法 BM 拟合） | 模量偏差 < 5% 且 4 seed 通过率 = 100%（#8a）；E 曲线残差 < 曲率信号 25%（#8b 相对判据） | ✅ |
| 9 | committee σ_F 外推率（测试集 σ_F > 3× 训练集中位 σ_F 的帧占比） | < 5% | ⚪ informational（分母仅 ~14 帧，5% 阈值等价 0% 闸，见 §9.1 ③ 与 benchmark.py 注释） |
| 10 | **模式 Grüneisen γ(q,ν) 对照 DFT**（±1% 应变 fc2，两边同法，本征矢匹配模式） | 主要支 γ MAE < 0.3 | ✅ |

**#1/#2 是 autoplex 的原始验收标准。**

**#3/#4 必须加**：力常数要在**势自身的能量极小点**上做泰勒展开。在 DFT 极小点上取 MACE
的力，残余力不为零，二阶力常数里就混进一次项，Γ 附近声学支直接掉成虚频——看起来像
「材料不稳定」，其实是流程错。所以 MACE 一侧先用微调模型自弛豫（FixSymmetry 挂全程，
否则数值噪声把空间群降到 P1、位移数膨胀），再取位移、拟 fc2。

**#10 是本套验收唯一的三阶敏感量，必须保留，且必须知道它的局限**：主指标全是二阶量，
而用 MLIP 的真正理由是 fc3 太贵（fc2 用 DFT 直接算也行）。**fc2 好不保证 fc3 好**。
Grüneisen 是 fc2 对应变的导数，是最便宜的三阶代理，但替代不了真正的 fc3 检验。
**本技能验收的是「势在谐波与准谐层面可信」；fc3 与 κ 的最终可信度要靠下游 `kl-mace-*`
跑出与已知参考对照，本技能不替它背书。**

`step8` 产出五张图：`<材料>_band_comparison.png`（DFT/MACE 声子谱叠画）、
`<材料>_rmse_phonons.png`（q 点逐点 RMSE，下一代定向加采靠它）、`energy_forces.png`
（E/F parity 三联：训练/测试/过滤）、`learning_curve.png`（两条曲线）、
`gruneisen_compare.png`（γ 对照散点）。

`results_<材料>.txt`（autoplex 一行摘要）：

```
Potential  Structure  Dim  Gen  Displacement(Å)  RMSE(THz)  imagmodes(pot)  imagmodes(dft)  Frames  Status  Hyperparameters
MACE-ft    Si         3d   0    0.01             0.183      False           False           25      pass    multihead,f_w=100,lr=1e-4,30ep
```

### 9.1 验收判据的常见坑（Si 实测踩坑记录，换材料必读）

> 下面这些**没有一个是物理问题，全是验收在报告它没有真正测量的东西**。
> 下次换材料（尤其 2D）重跑，不记下来就会再踩一遍。记判据，不记结论。

**① 指标要和被测信号的量级对齐（#8b 的教训）**

E(V) 曲率信号 `ΔE ≈ (9/8)·V0·B0·x²`，Si 在 ±3% 应变处只有 ~5 meV/atom，
与「E 曲线 RMSE < 5 meV/atom」同量级——一条完全平的 E(V) 也能过这个绝对阈值，
是空闸。改成相对判据：残差 < 曲率信号的 25%。**任何绝对阈值，先算被测信号的量级再定。**

**② required 闸必须看 seed 通过率，不是单模型（#8a 的教训）**

B0 对训练超参（sw）和随机 seed 都极敏感：sw 10→100 摆 33 GPa，4 个 seed 间摆 20 GPa
（90.9~110.5）。单 seed 的 PASS 可能是抽样左尾，换 seed 就 FAIL。
benchmark 现在对**所有 seed** 各算一遍 B0，通过率 < 100% 则 #8a 判 FAIL
（4 seed 时 90.9 这种 1/4 通过就是假阳性）。**任何 required 闸，4 seed 通过率 < 100% 判 FAIL。**

**③ 外推率 0.0% 是指标坏了，不是通过（#9 的教训）**

单 seed 时 committee σ_F 恒为 0，外推率 0.0% 是「委员会没在测任何东西」，不是模型好。
N_COMMITTEE=1 的验收里 #9 必须标注「单模型，无效」而不是 PASS。

**④ 超参调完之后，那些闸就不再能证明任何事**

sw 10→100 让 B0 摆 33 GPa，说明 B0 对超参极敏感。再去调 sw 找一个「两全」的点，
本质是拿验收集调超参——调完那些闸变成了目标函数本身，失去独立证明力。
要么用没参与调参的量判，要么接受当前值。

**⑤ 其他已修的同类坑（验收在报告它没测的东西）**

- `--loss` 传两次 / 硬编码 `--loss=weighted` 覆盖用户配置 → 应力项从未进损失，
  B0 偏 29% 却无人发现（mace_finetune.py 去掉硬编码）。
- G3 补丁 `vols_m[:_n_stat]` 假设 MACE 前 n 个 static 点和 DFT 一一对应，
  但 DFT 循环 `continue` 跳过没算完的帧 → 列表错位，B0 垃圾且不报错
  （已改显式 `statics_ok` 对齐 + 断言）。
- recipe 指纹漏 `force_mh_ft_lr`/`patience` → 改这两个键不触发重训，拿到旧模型
  还以为试了新配置（已补 `|mh%s|pt%d`）。
- `ck_publish` 缺代数检查 → gen-3 通过后未重新发布（已加）。
- 训练期必须看 pt_head：`force_mh_ft_lr=true` + LR=1e-3 时预训练头发散
  （末段 std ~700 且涨），seed 间 B0 散布 ~20 GPa；`force_mh_ft_lr=false`
  （MACE 官方策略）时 pt_head 末段 std ~24。**重训后先确认是早停停下的、
  Default head 末段已平，指标才有意义。**
- **POTCAR 与 POSCAR 的元素一致性（Ba1C20 首次踩，2D 双元素体系）**：
  S5 曾把所有 cfg 写成 step1 完整 POTCAR——多元素体系 = 多段，而 iso 帧（孤立原子）
  POSCAR 只有单元素，VASP 顺序取第一段 → **孤立 Ba 被当 C 算**（OUTCAR
  `VRHFIN=C`、NELECT=4.0，C/Ba 两 iso 帧能量完全一致），标签静默错。
  默认 `E0S_MODE=estimated` 根本没用 iso 帧算 E0s，**默认值掩盖了 bug**——
  直到 Ba1C20 训练才暴露（与「机制在报告它没有真正执行的东西」同族）。
  已修：`gen_step5_label.py` 按每帧 POSCAR 元素列表重拼 POTCAR + 断言段序一致
  （`poscar_elements`/`build_potcar_for_frame`/`potcar_element_syms`）。
- **超胞 POSCAR 符号行必须按 phonopy 同序聚拢（Ba1C20 首次踩）**：
  S4 的 static/rattle 曾用 image-major 展开（image 外层、原胞原子内层），多元素原胞
  （C20 Ba1）得到 `C Ba C Ba …` 交替序列，write_poscar 相邻合并写出 18 段符号行，
  VASP 判 18 species 而 POTCAR 只有 2 段 → 直接 ERROR 崩（单元素 Si 无感）。
  已修：`rattle_gen.py` 展开改 atom-major（原胞原子外层），与 phonopy 的 displ
  超胞同序；displ 帧本就由 phonopy 生成（atom-major），不要动那一路。
  **教训：改原子顺序前先确认 phonopy/ASE 的排序语义，fc2 拟合静默吃顺序错。**
- **大超胞 DFT 单点用 GPU 模板（Ba1C20 实测）**：S5 单点（IBRION=-1 不变胞）
  不需要 IOPTCELL，但曾继承 S1 2D 弛豫的 optcell CPU 模板 → 189 原子超胞
  12 核 CPU 首步 SCF ~3h 还没出第一个 DAV（LREAL=F 倒空间 + ENCUT=600 极贵）。
  换 GPU 版 vasp.6.6.0（gpu 分区 1 rank + gres:gpu:1）后 ~40s/电子步、十几分钟
  收敛。做法：`project_setting/templates/<步骤>/submit_std_2d.tpl` 项目级覆盖 +
  step.conf `[submit] ntasks_per_node=1`（GPU 1 rank/卡）。**大超胞单点先想清楚
  跑哪台机器/哪种 vasp，别被「继承模板」坑掉几十个机时。**

### 9.2 随机源未固定 = seed 扫描测不出方差（Si 实测，最隐蔽的一类坑）

> 结论之外的判据。以下每一条都来自 Si 实测，记**判据**不记结论。

**① 同 seed 同命令行跑两次，差异必须为零；不为零说明有未固定的随机源。**

Si 上 `multiheads_finetuning=true` + `num_samples_pt=1500`、`--seed=1` 完全相同的
两次训练，B0 一次 94.3、一次 115.5（差 20 GPa），Default head loss 0.0140 vs 0.0211。
根因：`num_samples_pt` 的 replay 随机采样**没有固定随机种子**，每次采到不同的子集。
**结论：此时任何 seed 统计都是「给定 replay 子集」的条件方差，不是总方差。**
mh1500 的 4-seed 极差 2.2 GPa 测的是同一次 replay 抽样内部的散布，换一次抽样整体平移 20 GPa
——seed 极差这个指标在多头模式下**根本没在测真正的方差源**（又一个
「指标在报告它没有测量的东西」的实例，与 committee 假 0%、#8a 单 seed 假阳性同类）。

**标准动作（便宜，抓到最难发现的一类错误）**：任何要信 seed 统计之前，先同 seed
同配置跑两次，差异应为零。不为零 → 先修随机源，再谈 seed 统计。

**② replay 与目标数据的比例是主控变量。** MACE 开发者建议 replay:微调 ≈ 10:1
**偏向目标数据**；而 `num_samples_pt=30000` 对百帧量级的数据集是 ~200:1 **反向**，
pt_head 主导梯度、Default head 饿着。Si 实测 B0：多头 30000 → +21.5%，naive → +2.6%。

**③ 单材料专用势用 naive 单头微调，不用多头 replay。** 本技能产物是交给
`kl-mace-cpu`/`phonon-mace-cpu` 算**单一体系** κ/声子的专用势，不需要跨体系泛化；
replay 头在这里纯粹消耗容量与梯度预算。naive 是窄目标应用的第一强基线
（系统 benchmark 结论），多头 replay 留给需要广泛分布外鲁棒性的场合（如长时 MD
探索未知构型）。Si 实测 naive 4-seed B0 极差 3.0 GPa、中心 97.6（DFT 95.1），可复现
（两次 97.2）。

**④ 多头路径没人走之后，别让它变成「没人测的死分支」。** `MULTIHEAD` 默认 false
后，多头是留是删要明确：留就标注「多头未经充分验证，启用前先做同 seed 复现检查
（见 ②）」；删就删干净，不留半活的分支。**半活分支比删掉更危险。**

**⑤ replay 采样无种子这件事，进 recipe 指纹也修不了。** 指纹一样但采样不同，幂等
检查会「配置没变、跳过」，而实际模型可能差 20 GPa。真要修：给 replay 采样传固定
种子（和训练 seed 绑定或单独一个键），并把它也进指纹。不修就至少把这一路的
不可复现性记进 `model_card.json`。

**⑥ PATIENCE 在 mace 单卡训练里完全不生效（上游 bug）。** mace-torch 0.3.16 的
`train.py` 里，非分布式时 `exit_now = None`，早停那段 `if exit_now is not None`
跳过 break——所以 `patience_counter >= patience` 只打印 `Stopping optimization after
N epochs without improvement`（从某 epoch 起每 27s 一条，一路打印到结束），**永不
跳出循环**。`PATIENCE` 写在 step.conf 里看起来在把关，实际完全无效，一定训到
`max_num_epochs`。

判断方法与影响：`grep "Stopping optimization" seed-*/train.log` 能看到触发 epoch；
`ls seed-1/checkpoints/` 里最后一个 `epoch-N.pt` 就是最后一次验证改善的 epoch（N），
真正的收敛点在 N 而不是 EPOCHS。**模型没受影响**——checkpoint 只在验证损失改善时
保存，训练结束 load 的是最佳 checkpoint（N），不是最后一个 epoch 的权重；声子谱/B0/
力这些数都是最佳点的，有效。代价只是白烧 GPU。

处置：EPOCHS 按「真实收敛点 N × 1.5」设，别指望 PATIENCE 提前停。`mace_finetune.py`
训练前会打印一行 mace 版本警告说明「单卡早停不生效」。这是又一个「机制在报告它
没有真正执行的东西」的实例——与 ①~⑤ 同类。

---

## 10. 下游怎么接（哪些下游吃哪些数据）

| 下游 | 吃什么 | 不吃什么 |
|---|---|---|
| `thirdorder.py`（ShengBTE 路线） | 只吃**对称性生成的特定位移超胞** | **不吃 MD 帧、也不吃 rattle 帧** |
| pheasy / hiPhive | 吃 rattle 快照（本技能 step6 的 xyz 就能喂） | — |
| `kl-mace-cpu` / `phonon-mace-cpu` | 本技能的 `<材料>_ft.model`（MACE_MODEL=文件名即可） | — |

要 fc2/fc3 根本不需要跑 MD——这正是本技能与 autoplex 的核心论点。

---

## 11. 参数表（step.conf 三层合并，全量）

见 `templates/step.conf`（出厂默认）。常用键：`FUNC / DIMENSION / ENCUT_OVERRIDE /
DATA_MODE / PRE_XYZ_FILES / REF_FC2_PATH / VOL_FACTORS / RATTLE_STD / N_PER_CELL /
MIN_DIST_RATIO / REF_DISP / ISO_BOX / MIN_ATOMS / MAX_ATOMS / MIN_VACUUM / GENERATION /
MAX_GENERATION / RMS_MAX / IMPROVE_MIN / GEN_INCREMENT / CURVE_POINTS / CURVE_TOL /
FORCE_CONTINUE / ENERGY_LIMIT / FORCE_LIMIT / KSPACING_TOL / MACE_MODEL / MACE_MODEL_DIR /
REPLAY_XYZ / N_COMMITTEE / ENERGY_WEIGHT / FORCES_WEIGHT / STRESS_WEIGHT / BATCH_SIZE /
DTYPE / LR / EPOCHS / DEVICE / N_GPU / KPOINTS_GRID / EDIFF / NCORE / CONDA_SH / CONDA_ENV`。
改：`tf -tt mlff-mace -p <材料> -j <步骤> conf --set params.KEY=值`。

---

## 12. 环境与「装」一节（需要你确认的清单）

- 集群 `jzzn` 登录节点**无外网**：任何联网路径都有本地 fallback + 可行动诊断。
- MACE：CPU 用 `~/venvs/mace_cpu`（torch 2.7.1+cpu、mace-torch 0.3.16、phonopy 2.47、
  hiphive 1.5、ASE 3.26、numpy、matplotlib，已验证）。**jzzn 无 GPU 分区**
  （`sinfo` 只有 cpu192*），`DEVICE=auto` 恒落 CPU；GPU 用法：`DEVICE=cuda` +
  `CONDA_ENV` 指向 mace-gpu 环境 + 换 hpc 的 `submit_mace.tpl`（A800 的 setting/a800
  未在本集群配置，GPU 路径未实测）。EPOCHS=1500 上限 + PATIENCE=100 早停是 autoplex
  配方，CPU 上单 seed 约 1~3 小时——正常。
- 集群设了 `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD`。
- 提交模板逻辑名：`submit_std_2d.tpl / submit_std_3d.tpl`（VASP，12 核由 step.conf
  `[submit] ntasks_per_node=12` 覆盖）、`submit_mace.tpl`（step7/8）。
- 基座模型：默认 `MACE_MODEL=MACE-matpes-pbe-omat-ft.model`（OMat24 系 PBE 微调，
  **Si 实测远好于 MP-0**，§0.1 ①；下载见
  [mace-foundations releases](https://github.com/ACEsuit/mace-foundations/releases/tag/mace_matpes_0)）。
- replay：`REPLAY_XYZ` 只在 `MULTIHEAD=true` 时必需（§8 ③④）；naive 单头不需要。
- 运行时检测：step3 缺 hiphive / 缺 phonopy / 基座模型不覆盖元素 / replay 缺失都会带
  可行动错误信息退出。

---

## 13. 已知局限（写进 model_card）

1. 验收的是谐波/准谐层面；fc3 与 κ 由下游背书。
2. 磁性体系：MACE 无自旋自由度，模型只对训练磁序有效。
3. 2D：未考虑 LO-TO 劈裂；面外应力未参与训练。
4. commensurate q 分辨率受基准超胞大小限制，长波行为检验偏弱（差异清单 #1）。
5. **只在 Si 上完整跑通过一次**（单元素、高对称、基座样本最密）；2D 分支（应力归零 / E(面积) / ZA 闸）一次没真实跑过。
6. **所有绝对阈值在 Si 上标定**：换体系先重算被测信号量级再定阈值（§0.1 边界，§9.1 ①）。
7. **数据集偏小的后遗症**：验证集 ~12 帧使 `lowest_loss` 部分靠噪声刷新（seed4 末段改善到 1192 是噪声）；测试集 14 帧使 #9 外推率分辨率不足（1 帧超阈即 7.1% > 5%）。
8. **单卡早停不生效**（mace-torch 0.3.16 上游 bug）：`PATIENCE` 只打日志不跳出，`EPOCHS` 即实际轮数；模型取最佳 checkpoint，结果有效但白烧 GPU（§9.2 ⑥）。

---

## 14. 自检清单核对结果（SKILL_DEV §10）

- [x] 目录自包含：gen_need 列的文件全部在 `skill/mlff-mace/` 下（`dim_common.py /
      stepconf.py / relax_common.py / mace_model.py / mace_relax.py` 从 `_common` 拷入，
      其余为本技能自写）；无跨目录 import（relax_common 的 [MLFF] 补丁 import 同目录
      mlff_common）。
- [x] 依赖清单写在步骤级，每步列全提交模板逻辑名（submit_std_2d/3d、submit_mace）。
- [x] schema/name/desc/steps 齐全；9 步。
- [x] 每个 steps[].name 与 gen 脚本 OUTDIR 逐字相同（step1_relax … step9_publish）。
- [x] 每步有 seq/label（≤10 字符）/check/gen。
- [x] 判据：能内置就内置（step2/3 用 plot+done_marker）；数值/代数判据在 checks.py
      （纯标准库、无顶层副作用、判据名不与内置重名、只读 json/尾部文本，秒级）。
- [x] gen 脚本：cwd=材料目录；结构接力显式检查（CONTCAR/summary 缺失即 sys.exit）；
      非零退出码语义清晰（[ERROR] 前缀）。
- [x] 提交模板只写逻辑名，不写 submit_jzzn_*；12 核由 step.conf [submit] 覆盖实现
      （hpc 集中模板优先级高于技能目录，写技能内副本会被遮蔽——差异清单不单列，
      设计说明见 step.conf 注释）。
- [x] run:gen 步骤写 run: gen + check + done_marker（step2/3/4/6/9）。
- [x] 扇出步骤（step5 cfg-*、step7 seed-*）：gen 幂等（不删已算产物、模型存在即跳过），
      子目录名与 fanout glob 对得上，每个子目录有自己的 submit.sh。
- [x] defaults.skill_subdir: true。
- [x] `tf skills` 无关于本技能的警告（0.1 版，9 步，已实测）。

---

## 15. 实测记录（Si，完整走通一遍）

**最终结果（matpes 基座 + naive 单头，gen-3）：**

| 指标 | 值 | 目标 |
|---|---|---|
| 声子谱 RMSE | **0.073 THz** | < 0.20 |
| B0（4/4 seed） | 97.1/96.5/97.3/94.3，极差 3.0，中心 96.2 | 95.1±5% |
| 力 RMSE | 47.6 meV/Å（相对 2.2%） | < 3% |
| 能量 RMSE | 0.55 meV/atom | < 3 |
| Grüneisen γ MAE | 0.12 | < 0.3 |
| #9 committee | 14.3%（informational） | — |

**关键过程数据：**

- Si 金刚石原胞（2 原子）→ 超胞 4×4×4（128 原子，r_max=6.0 Å 读自基座）。
- 三代数据累计：gen-0 25 帧 → gen-1 21 → gen-2 21 → gen-3 49（VOL_FACTORS 3→7 点），
  最终 116 帧（train 102 / test 14）。
- 基座从 MP-0（未微调 B0=76.5，偏软 20%）换成 matpes/OMat24（105），微调后 96.2。
- CPU/GPU DFT 帧一致性：挑老帧 cfg-0-rattle-004 GPU 重算，能量差 8e-8 eV、
  最大力差 1e-5 eV/Å——数值上同一结果，混用无系统偏差。
- 训练：naive 单头（MULTIHEAD=false），sw=100、huber_delta=0.10、EPOCHS=1200（早停单卡
  不生效，见 §9.2 ⑥）、PATIENCE=200。

**这一路修掉的坑（都已写回代码/README）：**

- `--loss` 传两次/硬编码 weighted 覆盖用户配置 → 应力项从未进损失（已去掉硬编码）。
- #8b 绝对阈值是空闸（改相对判据）；G3 补丁 static 帧列表错位（改 statics_ok 对齐+断言）。
- committee 假 0%（单 seed 时指标失效）；#8a 单 seed 假阳性（改 4 seed 通过率）。
- recipe 指纹漏 force_mh_ft_lr/patience/num_samples_pt/multiheads（已补全）。
- replay 采样无种子 → 同 seed 两次 B0 差 20 GPa（改 set_seeds 加 cuda seed + README §9.2）。
- PATIENCE 单卡早停不生效（mace-torch 0.3.16 上游 bug，mace_finetune 加版本警告）。
- 其他：EARLY_EXIT 跳过变胞段、VASP 6.4 TOTAL-FORCE 六列、in-kB 应力符号、phonopy 2.47 API。
