# skill/el —— 弹性 / 力学性能计算技能

在 taskflow 的 bd（能带）技能同一套框架上扩展的力学性能流水线。输入一个 POSCAR
（原胞或任意标准胞，2D/3D 皆可），自动完成：**标准化 → 弛豫 → 弹性常数 → 力学性能+出图**。
2D/3D 全程自适应；输出体/剪切/杨氏模量、泊松比、Born 稳定性、Pugh/Cauchy、硬度、
各向异性、Debye 温度、声速，2D 则给面内刚度(N/m)与 Y₂D/ν₂D。

---

## 一、文件清单（12 个，无多余文件）

| 文件 | 作用 |
|---|---|
| `gen_step1_std_opt.py`      | 生成 step1 输入：结构标准化(IEEE) + ISIF=3 弛豫（2D 自动多段） |
| `step1_check_and_resubmit.py` | step1 收敛检查 + 自动重投（轨迹感知、2D 感知） |
| `gen_step2_elastic.py`      | 生成 step2 输入：IBRION=6 有限差分弹性常数 |
| `step2_check_and_resubmit.py` | step2 检查（弹性张量是否算出）+ 重投 |
| `gen_step3_postprocess.py`  | step3 本地后处理：解析张量→力学性能→出图（不提交 SLURM） |
| `check_common.py`           | 静态类步骤的检查+重投共享库（step2 用；bd 原件，勿改） |
| `dim_common.py`             | 2D/3D 自动识别 + 模板选择（bd 原件，勿改） |
| `incar_3d.tpl` / `incar_2d.tpl` | step1 弛豫 INCAR 模板（按维度自动选） |
| `submit_jzzn_vaspstd_3d.tpl` / `submit_jzzn_vaspstd_2d.tpl` | 提交模板（2D 用 optcell 版 VASP） |
| `README.md`                 | 本文件 |

> 轨迹诊断（判震荡）已**内联进 `step1_check_and_resubmit.py`**，不再单独放 `relax_diag.py`。
> 若你希望 tf 主程序也共用同一份诊断，可把该段抽成模块两边 import；单技能自用则内联即可。

---

## 二、每个脚本的输入 / 输出

所有 `gen_*` 在**材料父目录**运行（目录里需有 `POSCAR` + 两类 `.tpl` + `dim_common.py`）。
所有 `*_check_*` 脚本：**stdout 一行 JSON**（供 agent/tf 解析），**stderr 人类日志**，
退出码 `0 收敛 / 10 已重投或未收敛 / 20 运行中 / 30 停手 / 40 出错`。

### gen_step1_std_opt.py
- **输入**：材料目录下的 `POSCAR`；`incar_{2d,3d}.tpl`、`submit_std_{2d,3d}.tpl`、`dim_common.py`。
- **输出**：`step1_std_opt/` 内 `POSCAR`(已标准化)、`INCAR`、`KPOINTS`、`POTCAR`、`submit.sh`、
  `workflow_method.txt`(记 DIM/泛函)；2D 还有多段文件 `run_relax.sh` + `INCAR.s1_isif2/s2_isif3/s3_finish`
  和 `OPTCELL`（optcell_file 流派时）。
- **顶部可调**：`STD_CELL`(primitive_standard/conventional)、`TWO_STAGE_2D`、`FINISH_IBRION1`、
  `MANUAL_ENCUT`/`ENCUT_FACTOR`、磁性/U 表、`SUBMIT_OVERRIDE={"nodes","ntasks_per_node","qos"}`。

### step1_check_and_resubmit.py
- **输入**：`step1_std_opt/`（读 `OUTCAR`/`OSZICAR`/`CONTCAR`/`INCAR`/`workflow_method.txt`）。
- **输出**：一行 JSON（含 `status`/`verdict`/`dimension`/`external_pressure_kb`/`n_ionic_steps`…）；
  未收敛且还在下降时**自动 cp CONTCAR POSCAR 重投**并留档 `run_NN_*.tar.gz`。
- **判据**：读离子步能量轨迹诊断 `progressing/oscillating/stalled/thrown/electronic/nsw`：
  力达标+压力达标→收敛(0)；停滞/小振荡→有效收敛进下一步(0)；仍在下降→续算(10)；
  撞NELM/被甩飞/大振荡→停手交人工(30)。**2D 忽略外压**（冻结 c 轴残余压属正常）。
- **常用参数**：`--pressure-tol 5`（3D）、`--max-restarts 3`、`--check-only`、`--no-accept-stalled`。

### gen_step2_elastic.py
- **输入**：`step1_std_opt/` 的 `CONTCAR`+`INCAR`+`POTCAR`+`workflow_method.txt`。
- **输出**：`step2_elastic/` 内 IBRION=6 的整套输入（继承 step1 的 ENCUT/GGA/磁性/U，注入
  IBRION=6/ISIF=3/NFREE/POTIM/NCORE=1）。
- **顶部可调**：`POTIM`/`NFREE`/`EDIFF`/`KSPACING`、`INCAR_SET_EXTRA`/`INCAR_REMOVE_EXTRA`、`SUBMIT_OVERRIDE`。

### step2_check_and_resubmit.py
- **输入**：`step2_elastic/`（`OUTCAR`）。
- **输出**：一行 JSON；收敛判据 = 电子自洽收敛 + 正常收尾 + **OUTCAR 出现 `TOTAL ELASTIC MODULI`**。

### gen_step3_postprocess.py（本地运行，不提交 SLURM）
- **输入**：`step2_elastic/` 的 `OUTCAR`+`POSCAR`(+`workflow_method.txt`)。
- **输出**：`step3_postprocess/` 内
  `elastic_tensor.json`（IEEE 框架 C_ij，GPa；2D 为 N/m）、
  `mechanical_properties.json`（B/G/E、ν、Pugh、Cauchy、硬度、各向异性、Debye、声速；2D 为面内量）、
  `summary.txt`（人类可读）、
  `mechanical_anisotropy.png`（3D：E 在三晶面截面的极坐标图）/ `mechanical_anisotropy_2d.png`（2D：E₂D(θ)、ν₂D(θ)）。
- **顶部可调**：`FORCE_DIM`、`MAKE_PLOTS`/`PLOT_DPI`/`PLOT_3D_PLANES`、`HARDNESS_MODELS`。

---

## 三、2D / 3D 自适应（要点）

- **判定**：`dim_common` 按真空层自动判 2D/3D，step1 写进 `workflow_method.txt`，后续步继承。
- **step1 2D 多段弛豫**（`TWO_STAGE_2D=True`）：ISIF=3+optcell 对 2D 难收，故拆成
  `ISIF=2 清原子 → ISIF=3 放面内 → IBRION=1 收尾`，由 `run_relax.sh` 一个作业内串起、
  submit.sh 改调 `bash run_relax.sh`。**3D 恒单段 ISIF=3，不受影响。**
- **step1 收敛**：2D 不判外压（冻结 c 轴残余压正常），靠力+能量轨迹判。
- **step3 2D**：抽面内子块 × 真空轴胞高 → 2D 刚度(N/m)、Y₂D/ν₂D、二维 Born。

---

## 四、怎么跑

### 手动（在材料目录里）
```bash
python gen_step1_std_opt.py            # -> step1_std_opt/；sbatch step1_std_opt/submit.sh
python step1_check_and_resubmit.py     # 作业结束后查收敛（未收敛会自动续算/或停手）
python gen_step2_elastic.py            # -> step2_elastic/；sbatch step2_elastic/submit.sh
python step2_check_and_resubmit.py     # 查弹性张量是否算出
python gen_step3_postprocess.py        # 本地后处理 + 出图 -> step3_postprocess/
```

### 注册进 tf（`setting/tf.yaml` 的 `task_types:` 下）
```yaml
  elastic-dft-cpu:
    desc: 弹性/力学性能
    hpc: jzzn
    skill_dir: skill/el
    work_dir: <你的工作根目录>
    gen_need: [dim_common.py, incar_2d.tpl, incar_3d.tpl, submit_std_2d.tpl, submit_std_3d.tpl]
    aux_files: [check_common.py, step1_check_and_resubmit.py, step2_check_and_resubmit.py]
    steps:
      - {name: step1_std_opt,     label: S1_opt,     gen: gen_step1_std_opt.py,     check: outcar_relax}
      - {name: step2_elastic,     label: S2_elastic, gen: gen_step2_elastic.py,     check: outcar}
      - {name: step3_postprocess, label: S3_post,    gen: gen_step3_postprocess.py, check: plot}
```
> step3 是本地后处理、产 `.png`，用 `check: plot` 即可（存在 png/结果即完成）。
> `check_common.py`、`dim_common.py`、submit 模板都与 bd 通用；bd 侧升级时同步覆盖本目录。

---

## 五、一个已处理的坑

VASP OUTCAR 的弹性张量是 `XX YY ZZ XY YZ ZX` 顺序（**非**标准 Voigt），
`gen_step3` 内已按 `[0,1,2,4,5,3]` 重排后才交给 pymatgen，低对称体系不会错位。
