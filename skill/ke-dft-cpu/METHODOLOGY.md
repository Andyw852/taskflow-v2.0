# ke-dft-cpu 方法学（2D 电子输运复现）

> 复现 J. Appl. Phys. 139, 024301 (2026) doi:10.1063/5.0308349 Table I 的**方法学口径**。
> 本文件只含方法学：口径定义、根因分析、验收判据、参数表。
> 六材料对比表 / 相对误差 / AMSET 旁证等**未发表结果**见数据目录（不进 git）：
> `/mnt/d/tf_data/jzz/jap/comparison_vs_literature.md`。

## 1. 有效厚度（厚度口径）

二维体系的 σ、κ_e、κ_p 都 ∝ 1/t，t 为有效层厚。本项目取
vdW 口径 d = zspan + r_vdW(top) + r_vdW(bot)（S 1.80、Se 1.90 Å），
单层自动得 CrS₂ 6.54、CrSe₂ 6.94 Å，与文献的 6.53/6.94 差 0.15%
——说明文献单层用的是同一套口径。

**ZT 与 t 无关。** `step8.1` 的 `_resolve_kappa_L` 只读 kl 链的原始
元胞口径 κ，再乘 ke 侧的 c/t，使 σ、κ_e、κ_p 共用同一个 t；
分子分母的 1/t 抵消。kl 链自己的 `KAPPA_2D_THICKNESS` 不进入 ZT，
只影响 kl 单独报出的二维归一 κ_p。

**但绝对值依赖 t。** 文献超晶格取两单层平均 6.73 Å，而 vdW 自动
值为 6.97 Å（由较厚的 CrSe₂ 侧决定），两者差 3.4%。要与文献的
κ_p 37/53（SS）逐点对比，需在两处分别锁 6.73：
- `ke-dft-cpu` 的 `LAYER_THICKNESS`（决定 σ、κ_e，以及 8.1 报出的 κ_p）
- `kl-*` 的 `KAPPA_2D_THICKNESS`（决定 kl 自己报的 κ_p）

两者互不影响，各自锁各自的；漏锁其一只影响该侧的绝对值，不影响 ZT。

## 2. E1 真空对齐（根因）

- ionrelax 两段式：`INCAR.relax IBRION=2` + `INCAR.static IBRION=-1 LVHAR=.TRUE.`，
  取 band_edges.json 的 `E1_vac_*`。
- 成因：amset 的 `get_reference_energy` 用原子核处的平均静电势，对 2D slab
  不跟随真空能级——这正是本工作用 LVHAR + 真空对齐绕开的那件事。amset 没有原生
  真空对齐选项，**重跑 step8 也无法解决**（不是 ICORELEVEL 那次能修的）。
- 验收：真空对齐线性拟合的 `off ≈ 0`（对齐斜率残差）。这步修掉 amset 芯势口径
  约 24% 的 E1 误差。

## 3. m* 口径

文献用的是 **full-BZ 二次型拟合的 m_d**（DOS 有效质量），不是 3 点抛物拟合的 m*。
用后者 μ 会低一半。

## 4. ε 真空稀释（2D 介电）

step5_dielect 的 VASP 输出是 slab-in-a-box 介电（含真空稀释）。skill 的
`gen_step10_amset.py` 里 `_dielectric_2d_inplane` 已扣真空：
`ε_m = 1 + (c/t)(ε_slab − 1)`，c/t 复用弹性同款。所以 AMSET 用的 ε 不是被稀释的值。
真正剩下的偏差是 **3D 散射形式**（POP 三维 Fröhlich / IMP Brooks–Herring），
2D 下绝对值不可信、只作跨体系比值。

## 5. c 统一（比值法前提）

各材料元胞 c 轴统一 20 Å（有意为之），AMSET σ 的跨体系比值不含 c 差异伪影。
IMP/POP 扣真空、σ 归一化都依赖此前提；跨体系比值是唯一可用的口径，绝对值不作结论。

## 6. skill_rev 防呆

step8.x 的 `_SKILL_REV` 写进 comparison_summary.txt。改脚本即提交 GitHub 并 bump，
防陈旧副本（step12 / step9b / step13 三次被同一形状的 bug 咬过）。

---

> 结果文档（未发表，不进 git）：`/mnt/d/tf_data/jzz/jap/comparison_vs_literature.md`
