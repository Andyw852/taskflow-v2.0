# cohp-cogito 技能说明

## 用途

从 VASP 平面波计算出发，做**投影式成键分析**，输出：

- **ICOHP**（COHP 积分，eV/bond）—— 键能指标，负值代表成键
- **ICOBI**（晶体轨道键指数，elec/bond）—— 键级
- **Löwdin / Mulliken 轨道占据**（`all_atoms.json` 的 `onsite_occup`）
- **COHP 曲线**（`bond_cohp_plot.html`）与键网络可视化（`crystal_bonds.html`）

## 为什么用 COGITO 而不是 LOBSTER

**本工作实测：LOBSTER 5.1.1 与 VASP 6.x 的 WAVECAR 不兼容。**

9 轮独立对照实验（2026-09-11/12）：

| # | 配置 | electrons recovered | charge spilling |
|---|---|---|---|
| 1 | A₂B₂Te₅, VASP 6.6.0, NBANDS=48 | 18.6667 / 68 | NaN |
| 2 | A₂B₂Te₅, VASP 6.6.0, NBANDS=72 | 18.6667 / 68 | NaN |
| 3 | A₂B₂Te₅, VASP 6.4.3 | 18.6667 / 68 | NaN |
| 4 | A₂B₂Te₅, 显式 288 reciprocal KPOINTS | 18.6667 / 68 | NaN |
| 5 | A₂B₂Te₅, 去掉 Pb 5d 基函数 | 12.8333 / 68 | NaN |
| 6 | **Si 金标准（最简单体系）** | **0.0000 / 8** | NaN |
| 7 | Si 单线程 | 0.0000 / 8 | NaN |

**决定性证据**：连 Si 都只恢复 0/8 电子，`charge spilling` 恒为 `-nan%`。
症状为 Pb 5d 投影全 0、所有 s 轨道仅为期望值 ~1/4。判定为**二进制与
VASP 6.x 的 WAVECAR 格式不兼容**，与材料、参数无关
（VASP 版本、NBANDS、KPOINTS 格式、基组、单线程均已逐一排除）。

**COGITO 同一份 WAVECAR 的结果**：

| 指标 | LOBSTER 5.1.1 | COGITO |
|---|---|---|
| electrons recovered | 0 / 8 | **8.0 / 8.0** |
| charge spilling | NaN | **0.6%** |
| Si–Si ICOHP (2.37 Å) | −0.00000 | **−7.48 eV/bond** |
| 质量检查 | 失败 | within expected range |

## 上游要求

由 `band-dft-cpu` 的 `step3_PBE_WAVECAR` 满足：

| 要求 | 设置 | 说明 |
|---|---|---|
| `NSW=0` | 静态 | COGITO 只吃静态波函数 |
| **`ISYM=1/2/3`** | **约化网格** | **最关键**：`ISYM=-1` 会让 k 点重构失败 |
| `LWAVE=.TRUE.` | 保存波函数 | |
| **`NBANDS=(12–20)×natoms`** | 9 原子 → **144** | 不足会导致投影质量下降 |
| 无 `LSORBIT` | 非 SOC | COGITO 不支持自旋轨道耦合 |

在 band-dft-cpu 项目的 `templates/step3_PBE_WAVECAR/step.conf` 写：

```ini
[params]
SOC = False
NBANDS = 144
ISYM = 2
```

## 使用

```bash
# 1) 跑完 band-dft-cpu 到 step3（PBE 静态 + WAVECAR）
tf -tt band-dft-cpu -p Pb2Sb2Te5 start          # 逐步推进到 S3_WAVECAR

# 2) 切到本技能
tf -tt cohp-cogito -p Pb2Sb2Te5 start           # S1_COGITO -> S2_analyze -> S3_post

# 3) 看结果
tf -tt cohp-cogito -p Pb2Sb2Te5 fetch
cat <result>/step3_post/icohp_report.md
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `NUM_OUTER` | 4 | 轨道优化外循环；收敛告警时 +1/+2 |
| `DENSIFY` | 空 | COHP 曲线加密（2/3 更平滑） |
| `CONDA_SH` / `CONDA_ENV` | jzzn 的 `atomate2_p_a` | COGITO 运行环境 |

## 安装 COGITO（新集群）

COGITO 是纯 Python 包，但**目标集群常常无外网**。离线安装流程：

```bash
# 在有外网的机器上
pip download cogito-dft -d wheels/ --no-deps
# 或直接取 wheel：
#   https://pypi.org/pypi/cogito-dft/json  -> urls[].url

# 传到集群后
python3 -m pip install --no-index --no-deps wheels/cogito_dft-*.whl
```

依赖：`numpy`, `scipy`, `pymatgen`（集群上一般已有）。

## 性能参考（jzzn 登录节点，9 原子胞，144 能带，38 个 k 点）

| 阶段 | 耗时 |
|---|---|
| COGITO（单独跑） | ~23 分钟 |
| COGITO（3 个并行） | ~65 分钟/个（CPU 竞争） |
| COGITOanalyze | < 1 分钟 |
| COGITOpost | ~5 分钟 |

主要耗时在 Wannier 轨道生成；建议**串行**或最多 2 个并行。

## 实测结果参考（A₂B₂Te₅ 四材料，PBE-D3(BJ)）

短/长键 ICOHP 分化：

| 材料 | 短键 | 长键 | 比值 | ΔICOHP |
|---|---|---|---|---|
| Pb₂Sb₂Te₅ | Te–Sb 3.02 Å, −1.5674 | Te–Sb 3.17 Å, −0.5892 | 2.66× | 0.978 eV |
| Sn₂Sb₂Te₅ | Te–Sb 3.01 Å, −1.4589 | Te–Sb 3.18 Å, −0.5347 | 2.73× | 0.924 eV |
| Pb₂Bi₂Te₅ | Te–Bi 3.08 Å, −1.7450 | Te–Bi 3.25 Å, −0.7103 | 2.46× | 1.035 eV |
| Sn₂Bi₂Te₅ | Te–Bi 3.07 Å, −1.6073 | Te–Bi 3.26 Å, −0.6500 | 2.47× | 0.957 eV |

投影质量：charge spilling 0.26–1.01%，orbital mixing 0.37–0.45%。

## 限制

1. **不支持 SOC**：需要 SOC 的成键分析需另寻工具（LOBSTER 宣称支持但本环境不可用）。
2. **泛函取决于上游**：本技能只做投影，泛函级别由喂进来的 WAVECAR 决定（本工作为 PBE-D3(BJ)）。
3. **绝对值的可比性**：COHP 是投影方法，绝对值依赖基组定义；**跨材料比较比绝对值更可靠**。
4. **收敛告警**：COGITOanalyze 可能报轨道半径变化 >1%，此时建议 `NUM_OUTER` +1/+2 重跑 S1。

## 引用

- COGITO: https://github.com/olipemil/COGITO-dft （`pip install cogito-dft`）
- 相关背景：Maintz et al., *J. Comput. Chem.* (LOBSTER 原始方法)

## 静态绘图与输入规范（2026-09）

S1 的唯一计算输入是上游 band-dft-cpu/step3_PBE_WAVECAR：必须同时有 POSCAR、POTCAR、OUTCAR、vasprun.xml、WAVECAR，并满足 NSW=0、ISYM=1/2/3、LWAVE=.TRUE.、NBANDS >= 12*NIONS、不含 LSORBIT。S1 完成后必须保留 bond_info.txt、error_output.txt 和 COGITO 生成的 bond_cohp_plot.html。

绘图输入必须是 HTML 内嵌的 Plotly 数值数组（能量 x、COHP y，以及可选的键/轨道 customdata）。禁止读取旧 PNG 取点、在旧图上叠画、平滑或归一化。S2 应将数组导出为 cohp_traces.csv，并用 Matplotlib 新建画布生成 cohp_plot.png；若 HTML 缺失或数组无法解析，必须明确报错/告警并标记图未生成。

输出应至少包括：icohp_table.json、icohp_short_long.json、icohp_report.md、cogito_summary.json、cohp_traces.csv 和 cohp_plot.png。轨道分辨分析需保留每条 trace 名称；Bi 等体系出现 d 轨道时不得只用 s/p 分量解释总 ICOHP。

## 上游自动协调

COHP 不独立生成波函数。它将 and-dft-cpu 的 step3_PBE_WAVECAR 作为唯一上游。运行 	f -tt cohp-cogito -p <材料> start 时，生成器先查找同一项目的合格 step3；若不存在，则自动执行 	f -tt band-dft-cpu -p <材料> start 推进上游，并退出等待状态。上游完成后再次执行 COHP 即可继续。已有 step3 仍会重新检查上述静态、对称性、波函数和能带数要求；不合格时不会强行使用。
