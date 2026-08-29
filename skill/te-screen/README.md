# te-screen —— 热电快速替代筛选技能

对**全新生成的结构**，在不跑任何 DFT 的前提下，用 Ridge 替代模型把 14 个"易算特征"
（组成 magpie + 晶格几何）映射成 n/p 型 ZT_e 与 log10(PF)，作为多智能体闭环里的 T0 快速评分层。

## 物理链（两步，都是 `run: gen`，登录节点秒级、不占 GPU）

```
S1_features  读材料目录 POSCAR -> 14 维易算特征 -> step1_features/te_features.json
S2_predict   读 S1 特征 -> Ridge 预测 -> step2_predict/te_screen_summary.json
```

## 特征（14，无 DFT）

组成(magpie)：electronegativity_mean/range, atomic_mass_mean/max, atomic_radius_mean, Z_mean,
ionization_energy_mean, electron_affinity_mean, row_mean, group_range；
结构：density, n_sites, inplane_area, aspect(vacuum/√area)。

## 模型来源与精度

- 训练数据：JARVIS dft_2d（1103 二维材料），与 `matexplore/scripts/train_surrogate.py` 同源；
- 5 折 CV Spearman：ZT_e +0.35(n)/+0.49(p)，logPF +0.13(n)/+0.24(p)；
- 即：**第一轮粗筛够用，精确排名需回 band-dft-cpu + ke-dft-cpu 取 Eg/m\* 真值**。

## 使用

```bash
tf -tt te-screen -p <材料> hpc 3090     # 换跑 3090 登录节点(默认)
tf -tt te-screen -p <材料> -j 1 init   # 只生成输入不提交，先检查
tf -tt te-screen -p <材料> start        # 跑(登录节点 run:gen)
```

材料目录只需一份 `POSCAR`。输出 `te_screen_summary.json` 含 `prediction`（n/p 的 ZT_e 与 logPF）与 `merit`。

## 依赖

仅 `numpy`；`cheap_features.py`、`element_properties.json`、`model_*.json` 随技能自包含（`gen_need`）。
