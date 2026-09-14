# kl-dft-cpu —— 晶格热导率技能（v0.2 大改版）

对 v0.1（简化有限差分四步）的彻底重构。对齐你 band-dft-cpu/elastic-dft-cpu/ke-dft-cpu 的成熟架构：
`stepN_名字` 命名、per_step 模板布局（`templates/<步骤名>/`）、参数全部走 `step.conf`
（`stepconf.py` 解析、三层合并）。物理链对齐参考引擎 `lattice_kappa.py`，拆成 tf 异步分步。

## 相比 v0.1 新增/修复的四件事（你上次点名的）
1. **补了 NAC 步**（step3_nac）：LEPSILON DFPT 算 ε∞ 高频介电 + Born 有效电荷，供极性
   绝缘体 Γ 点 LO-TO 劈裂修正。金属自动提示（应关掉）。
2. **命名规范化**：step1_std_opt / step2_static / step3_nac / step4_disp / step5_fc /
   step6_kappa，每步职责写死在名字里。
3. **ALM 定位移数**：step4 的 `METHOD=alm` 用 ALM 的自由力常数个数 × `OVERSAMPLE` 估随机
   位移超胞数（复用参考引擎 `write_alm_suggest_input`/`run_alm`/`parse_alm_nfree`/
   `estimate_n_struct`），少而全，适合大超胞/高阶。
4. **声子拟合步**：step5 独立拟合力常数——findiff 用 `--sym-fc`，alm 用 `--alm`（压缩感知
   回归），再出声子谱做虚频闸。
5. **参数模板化**：每步关键参数写进 `templates/<步骤名>/step.conf`，gen 脚本只读这份 tf
   合成的文件，不再在脚本顶部写死。

## 六步流水线
| 步骤 | 名称 | 干什么 | 提交/本地 | 完成判据 |
|---|---|---|---|---|
| S1 | step1_std_opt | 结构优化（复用 band-dft-cpu/elastic-dft-cpu/ke-dft-cpu 同一脚本，一字不差） | vasp_std | OUTCAR 收敛 |
| S2 | step2_static | 原胞静态自洽（判金属、作声子参考胞） | vasp_std | OUTCAR |
| S3 | step3_nac | LEPSILON DFPT → ε∞ + Born（可选步，金属请关） | vasp_std | OUTCAR 介电张量行 |
| S4 | step4_disp | 超胞 + 位移生成，扇出单点取力 | vasp_std ×N | 每个 disp 的 OUTCAR |
| S5 | step5_fc | 收力 → 拟合 fc2/fc3 → 声子谱 → 虚频闸 | 登录节点 | phonon_summary.json stable:true |
| S6 | step6_kappa | BTE 解晶格热导率 | 计算节点 | kappa_summary.json KAPPA_DONE |

S3_nac 是可选步组：默认开；关掉在项目配置写 `nac: false`，tf 直接不注入 S3，S5/S6 因无
BORN 自然不加 NAC。

S1 的起始结构建议复用其它链已优化好的 CONTCAR：`python3 reuse_structure.py <材料名>`
（脚本在 `skill/kl-dft-cpu/`），按 ke → opt → band → elastic 顺序找候选 CONTCAR
（候选目录见 `_STEP1_CANDS`），命中就复制成**材料根 POSCAR**（tf 的 gen 一律从材料根取
初始结构，技能子目录里的 POSCAR 不被使用），覆盖前把原始 POSCAR 备份成 `POSCAR_raw`。
复用只是给 S1 更好的起始点（省离子步、更稳），**不跳过 S1**——S1 仍会以更严的收敛判据
（EDIFF=1E-7 + EDIFFG=-0.001）完整优化。材料根 POSCAR 是所有技能共用的初始结构，
覆盖影响所有未跑 S1 的技能（已跑完的不受影响）。

## 安装
把整个 `kl-dft-cpu/` 放到 `~/software/taskflow/skill/kl-dft-cpu/`，然后：
```
tf skills            # 应看到 kl-dft-cpu 0.2 / 6 步 / 启用
```

## 用法
```
tf -tt kl-dft-cpu -p <材料> init      # 生成 材料/kl-dft-cpu/project_setting/（含 templates/<步骤>/）
tf -tt kl-dft-cpu -p <材料>           # 看步骤链
# 按序推进各步（与你 band-dft-cpu/elastic-dft-cpu 一致的 advance/run 流程）
```

## 改参数（不要改脚本，改 step.conf）
```
tf -tt kl-dft-cpu -p <材料> -j <步骤> conf                    # 看某步最终生效值 + 来源
tf -tt kl-dft-cpu -p <材料> -j step4_disp conf --set params.METHOD=alm
tf -tt kl-dft-cpu -p <材料> -j step4_disp conf --set params.SUPERCELL="3 3 3"
tf -tt kl-dft-cpu -p <材料> -j step6_kappa conf --set params.KAPPA_MESH="24 24 24"
```

## 关键开关
- **METHOD**（step4_disp）：`findiff`（对称有限位移，默认，最稳）/ `alm`（随机位移 + ALM
  拟合，大超胞省帧）。
- **FC3_CUTOFF_PAIR**（step4_disp，findiff）：三阶对距离上限(Å)。**大超胞务必设 4~6**，否则
  全对称集位移数爆炸。
- **nac**（项目配置）：金属置 `nac: false`。极性绝缘体保持默认 true。
- **SOLVER**（step6_kappa）：`phono3py`（默认，完整支持 findiff/alm + NAC）/ `shengbte`。
- **SUPERCELL / MIN_SC_LEN**（step4_disp）：显式超胞或按最小胞长自动。2D 真空方向恒压 1。

## 环境（务必核对）
phono3py / alm 都在 conda 环境 `atomate2_p_a`。以下三处的环境路径要一致（按你集群改）：
- `kl_common.py` 的 `PHONO3PY_ENV_SRC`
- `templates/step6_kappa/submit_p3py.tpl` 里的 `conda activate`
- `templates/step6_kappa/submit_shengbte.tpl` 里的 `conda activate`

## 已知限制（需上机实测）
- **findiff + phono3py** 路线完整打通，是验证过的默认路径。
- **ALM 随机路线**依赖登录节点有 `ase` 和 `alm` 可执行文件；取不到时自动回退到
  `N=max(4, OVERSAMPLE×8)` 个随机位移并打印告警。
- **ShengBTE** 求解器：CONTROL 已能写出（复用参考引擎），但 fc3→ShengBTE 格式导出只有
  `random/hiphive` 路线可靠，`findiff` 的 compact fc3 无稳定导出口。`SOLVER=shengbte` 请配
  `METHOD=alm`，并在集群装好 ShengBTE、把 exe 填进 `step6_kappa` 的 `SHENGBTE_EXE`。
- **step5 力常数拟合在登录节点跑**；超大超胞的 fc3 拟合内存/耗时大时，可把 step5 的
  phono3py 拟合命令挪到计算节点（改成 submit 步）。

### ShengBTE 可执行文件（按集群，2026-09 实测）
- **jzzn（CPU 版）**：`/public/home/wangchao/software/sousaw-shengbte-aocl/ShengBTE`
  （官方 2024 内核 + AOCL，提交模板 submit_shengbte.tpl 已配套 module gcc/14.1 + openmpi/4.0.1 + aocl-gcc）。
- **3090（GPU 版）**：`/home/wangchaoyue852/software/taskflow/shengbte-gpu/ShengBTE`
  —— 即 **2020 HPC Asia 论文 CUDA fork**（仓库 buaa-hipo/ShengBTE-Multiplatform，克隆在
  `~/software/taskflow/shengbte-gpu/`，含 Src-gpu 源码）。已修复其 dGamma 未清零 bug，**phonopy fc2
  输入数值与官方 CPU 一致**（15³ RTA 8 温度点一致到 4~6 位有效数字），RTX3090 实测 ~4.6× 加速。
  运行需 `~/software/taskflow/shengbte-gpu/run_env.sh` 设 NVHPC/OpenMPI/CUDA12.4/spglib/OpenBLAS 环境，
  经 SLURM 提交：`sbatch gpu 分区 + --gres=gpu:N + NVHPC mpirun -n N`（每 rank 自动 cudaSetDevice(rank%8)）。
  ⚠️ 该 fork 基于 2019 官方内核，仅三声子；数值与 jzzn 2024 CPU 版可能有个位数 % 级差异（2026-09 Si 实测 <0.1%）。
- **3090（官方 v1.3 CPU，可选）**：`/home/wangchaoyue852/software/taskflow/fourphonon-v13/bin_cpu`
  （FourPhonon 官方主线 v1.3，ShengBTE 超集含四声子；GPU 版 ShengBTE_gpu 对 phonopy fc2 输入存在
  官方 OpenACC bug——κ 错 67~71×，尚未修复，勿用于 phonopy 数据）。

### fourphonon（ShengBTE 同源引擎）多卡加速 —— 2026-09 实战验证
step6 的 SOLVER=fourphonon 用它跑 3ph RTA（four_phonon=F，不需 fc4；输入 = 与 shengbte
相同的 ShengBTE 格式 FORCE_CONSTANTS_2ND/3RD，拷自 S5_fc/shengbte/，需 S5 的
EXPORT_SHENGBTE=true）。只建议在 GPU 机器多卡并行时选它；CPU 机请用 shengbte。

**已固化的 multi-GPU 版**（源码 + patch 在 ~/software/taskflow/fourphonon-v13-test/）：
- 可执行文件：…/ShengBTE_gpu_multigpu_test（NVHPC OpenACC -acc -gpu=cc86 -DGPU_VERSION
  -DGPU_ALL_MODE_PARALLELIZATION）
- 两个源码修复（相对官方 v1.3）：
  1. multi-GPU 分块：processes.f90 的 RTA_driver_GPU_using_Ind 从「仅 rank0 单卡」改为
     「每 rank 算 mm 连续段 + MPI_ALLREDUCE(MPI_IN_PLACE,…) 归约」——官方 mpirun -n 1
     单 rank 枚举段极慢（62 原子 12³ 枚举 >5h GPU 恒 0%）。
  2. 自动选卡：ShengBTE.f90 在 MPI_Init 后 call acc_set_device_num(myid, acc_device_nvidia)
     ——官方版依赖 CUDA_VISIBLE_DEVICES 手动分卡，漏设则全 rank 挤 GPU0。
- 验证：2³ 网格 1 卡 vs 4 卡 RTA 逐位一致；rank 数 = GPU 数（≤8），2³ 枚举 4 rank 比
  1 rank 快 ~10×（116s→11s）。
- 提交：templates/step6_kappa/submit_fourphonon.tpl（step.conf 配 FOURPHONON_EXE 绝对路径、
  FOURPHONON_NGPU=卡数、FOURPHONON_CPUS_PER_GPU=每卡核数，默认 8——枚举段 CPU 密集吃带宽，
  8 核/卡能明显缩短 GPU 点火前等待）。模板要求 NVHPC OpenMPI + CUDA + spglib/openblas。

**fourphonon 大胞坑（2026-09 Mg2C60 62 原子实测）**：
1. 别用 CPU 版跑大胞：枚举 11.8 亿通道（12³），64 核 CPU 都要 6.6h；single-rank GPU 更慢。
2. 别用 AOCC/flang 编译版：jzzn 上 AOCC 版跑 62 原子 12³ 在 sg 段后 12h 零进展卡死
   （03:51 后无日志无文件），已停；intel/NVHPC 编译版正常。
3. intel 2019 mpiexec.hydra 部分机器损坏（连 mpirun hostname 都 FPE）——换 NVHPC OpenMPI。
4. 官方 GPU 版 convergence=T（迭代解）未移植 GPU；即便 CPU 迭代解，62 原子 12³ 在 100K
   发散（相对变化卡 2.088、κ 爆 0.218E73，570+ 次迭代白烧 18h）。用 RTA（convergence=F）。


## 另：提醒一个与本技能无关的坑
你真实 `setting/tf.yaml` 里 `ke-dft-cpu:` 段的 `work_dir` 缩进是 2 空格（应为 4 空格），会让它变成
`task_types` 的同级字符串键，`tf -tt <任意>` 会抛
`dictionary update sequence element #0 has length 1; 2 is required`。把那一行改成 4 空格缩进即可。
（本次在干净仓库里跑 `tf -tt kl-dft-cpu` 无此报错，印证问题在该缩进。）
