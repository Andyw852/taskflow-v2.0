# MACE 参考化学势（μ）计算 —— 本地固化脚本

## 这是什么
为形成能计算提供每个元素的**参考化学势 μ**（= 该元素单质固体平衡态每原子能量，用同一 MACE 模型算出）。

形成能：`E_form = E_tot − Σ n_i·μ_i`，μ 就是公式里的"参考态能量"。E_form < 0 = 相对参考态（石墨碳 + 块体金属）热力学稳定。

## 为什么在本地跑
脚本很小（38 个金属小胞 + 1 个 α-Mn 58 原子），本地 CPU 几分钟就跑完，无需走超算排队。模型文件用 taskflow 技能自带副本（kl-mace-cpu/templates/mace/MACE-matpes-pbe-omat-ft.model，与超算一致）。

## 用法
```bash
bash setup_local.sh   # 第一次：建 venv（torch CPU + mace-torch + ase）
bash run_mu.sh        # 跑 38 金属，产出 results.json + 打印 MU 单行
```

## 产物
- `results.json`：逐元素 `E_per_atom`（eV/原子）、晶型、晶格常数、收敛标志。
- MU 单行：`MU = C:-9.1757 Ag:-2.70787 ... Zr:-8.51841`（C 沿用石墨参考 -9.1757，与 cages 项目同模型自洽），可直接粘到 `project_setting/templates/step3_formation/step.conf` 的 `MU =` 行。

## 已算好的值（2026-08-24，全部收敛）
```
MU = C:-9.1757 Ag:-2.70787 Al:-3.73173 Au:-3.22376 Ba:-3.21414 Be:-3.74085 Ca:-1.93444 Cd:-0.74187 Co:-7.02738 Cr:-9.48084 Cs:-0.81990 Cu:-3.74542 Fe:-8.26611 Hf:-9.89008 Ir:-8.83141 K:-1.01689 Li:-1.91443 Mg:-1.50700 Mn:-8.95644 Mo:-10.87294 Na:-1.30828 Nb:-10.12211 Ni:-5.47472 Os:-11.21275 Pd:-5.22010 Pt:-6.09433 Rb:-0.90332 Re:-12.41836 Rh:-7.24567 Ru:-9.23829 Sc:-6.23570 Sr:-1.63899 Ta:-11.85307 Ti:-7.77613 V:-8.96171 W:-12.91855 Y:-6.43111 Zn:-1.12144 Zr:-8.51841
```

## 注意
- 换 MACE 模型后 μ 必须重算（模型相关的值），脚本加 `MACE_MODEL_PATH=/路径/模型.model bash run_mu.sh` 即可。
- 金属晶型/晶格常数在 calc_mu.py 的 `STRUCT` 表里，改结构后重跑。
