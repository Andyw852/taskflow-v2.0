# opt-dft-cpu —— 结构优化 + 能量（形成能 / 嵌入能）

独立的结构优化技能：把 POSCAR 弛豫到位、算静态总能，再按参考能量换算成
形成能 / 嵌入能，用于高通量筛选的相对稳定性排名。

```
S1_opt      结构弛豫（复用公共池 relax_common，三段式作业内分段，0D/2D/3D 自动判定）
S2_static   静态自洽（读 S1 的 CONTCAR 接力，输出体系总能 E_tot）
S3_energy   能量后处理（登录节点，读 E_tot + 组分 + step.conf 参考值 → energy_summary.json）
```

## 用法

```bash
# 在含 POSCAR 的材料的上级目录，或项目目录里
tf -tt opt-dft-cpu init                    # 生成 project_setting/
tf -tt opt-dft-cpu -p <材料> start         # 推进（先 gen 再提交）
tf -tt opt-dft-cpu -p <材料> status        # 看状态
```

第一步结束后 `S3_energy` 会自动算能并把 `energy_summary.json` 拉回本地。

## 相对稳定性判据（高通量筛选）

`S3_energy` 产出这几个量（都写进 `energy_summary.json`）：

| 量 | 公式 | 要不要参考值 |
|---|---|---|
| `E_per_atom_eV` | E_tot / N_atoms | **不要** |
| `E_form_eV` / `E_form_per_atom_eV` | E_tot − Σ n_i·μ_i | 要（`MU`） |
| `E_embed_eV` / `E_embed_per_guest_eV` | E_tot − E_host − n_g·μ_g | 要（`GUEST_ELEMENT`/`HOST_ENERGY`/`MU_GUEST`） |

### 关键结论（回答"相对值够不够"）

**够，而且高通量筛选本来就用相对量。** 只要所有候选结构用**完全相同的
泛函/赝势/ENCUT/k 网格**算总能，参考值就是个常数，对所有候选整体平移，
不改变同一组成下的相对次序。

1. **同一组成（如各种 C20/C24 网络异构体）**：直接用 `E_per_atom` 排名即可，
   一个参考值都不用。更低 = 更稳定。
2. **不同组成（C20 vs C24 vs Li@C60）**：要归一化到每原子，再用形成能比较；
   参考化学势 μ_i 取**同一参考态**（如石墨 C、bcc Li）填进 `MU`。
3. **最严格的判据是"凸包上方的能量"（energy above hull, E_hull）**：把
   候选的形成能画进该化学空间的相图凸包，E_hull ≈ 0 才热力学稳定，< 0.1 eV/原子
   视为"可能合成/亚稳"。这一步需要 Materials Project 的相图数据，本技能只算
   形成能（E_hull 的原料），凸包比对建议用 `pymatgen`/MP API 离线做。

参考来源：
- Materials Project 的形成能 / 能量-凸包约定（formation energy、energy above hull）
  [mattermodeling.stackexchange.com](https://mattermodeling.stackexchange.com/questions/11546/energy-of-formation-vs-formation-energy-vs-heat-of-formation-vs-energy-above-hul)
- Li 嵌入能定义（相对 bcc Li 金属的化学势）
  [gpaw.readthedocs.io](https://gpaw.readthedocs.io/summerschools/summerschool26/batteries/batteries1.html#day-2-li-intercalation-energy)

## 参考能量怎么填（改 step3）

```bash
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set params.MU="C:-9.0 Li:-1.9"
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set params.GUEST_ELEMENT=Li
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set params.HOST_ENERGY=-1400.0
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set params.MU_GUEST=-1.9
```

- `MU`：元素化学势，单位 eV/原子。μ_C 常用石墨/石墨烯每原子总能，μ_Li 用 bcc Li 每原子总能。
- 参考结构要单独建材料、用同一套设置跑一遍，取它的 `E_per_atom` 或总能填进来。
- 不填参考值也不报错：`E_per_atom` 照常输出，形成能/嵌入能标为 `未算`。

## 参数

| 参数 | 位置 | 作用 |
|---|---|---|
| `FUNC` | step1_opt/step.conf | 泛函：pbe-d3 / pbesol / pbe（默认 pbesol） |
| `CALC_FORMATION` | step3_energy/step.conf | 是否算形成能（默认 true） |
| `CALC_INTERCALATION` | step3_energy/step.conf | 是否算嵌入能（默认 true） |
| `MU` / `GUEST_ELEMENT` / `HOST_ENERGY` / `MU_GUEST` | step3_energy/step.conf | 参考能量，见上 |

## 目录

```
skill/opt-dft-cpu/
├── skill.yaml                  流水线声明（S1_opt → S2_static → S3_energy）
├── gen_step1_opt.py            S1 薄壳（调公共池 relax_common）
├── gen_step2_static.py         S2 静态自洽（继承 S1 泛函/磁性）
├── gen_step3_energy.py         S3 能量后处理
└── templates/
    ├── incar_2d.tpl / incar_3d.tpl                S1 弛豫 INCAR 模板
    ├── submit_jzzn_vaspstd_2d.tpl / _3d.tpl       提交模板
    ├── step1_opt/step.conf                        S1 默认参数
    └── step3_energy/step.conf                     S3 默认参数
```

0D 模板（`incar_0d.tpl`、`submit_std_0d.tpl`）与 `relax_common.py`、`dim_common.py`、
`stepconf.py`、`mol_common.py`、`checks_relax.py` 都来自公共池 `skill/_common/opt/`，
本技能不再各自复制。
