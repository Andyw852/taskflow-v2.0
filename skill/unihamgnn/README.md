# unihamgnn —— Uni-HamGNN 通用 SOC 哈密顿量模型能带

用 Uni-HamGNN（HamGNN 通用模型，github.com/QuantumLab-ZY/HamGNN）预测材料的电子哈密顿量
矩阵，再对角化得到能带结构。不需要 VASP、不需要按体系重训：一个通用模型覆盖全周期表
（含自旋轨道耦合 SOC）。

> 出处：HamGNN 仓库 Uni-HamGNN/（通用 SOC 模型）与 DFT_interfaces/openmx/（结构→graph_data→能带）。
> 模型权重 .pkl 从 Zenodo 下载（records/17239078）。

## 流水线

| seq | 步骤 | label | 在哪跑 | 干什么 | 判据 |
|---|---|---|---|---|---|
| 1 | step1_graph_data | S1_graph | 计算节点 | POSCAR → OpenMX .dat → openmx_postprocess 产 overlap.scfout → graph_data_gen 产 graph_data.npz（non-SOC + SOC） | graph_data_summary.json 含 "GRAPH_DATA_DONE": true |
| 2 | step2_predict | S2_predict | 计算节点 | Uni-HamiltonianPredictor.py 预测 → hamiltonian.npy | predict_summary.json 含 "PREDICT_DONE": true |
| 3 | step3_band | S3_band | 登录节点（run: gen） | band_cal 对角化 → 能带图 + 带隙 | band_summary.json（plot） |

## 3090 实际部署（已装好，路径固化在 templates/step.conf）

| 组件 | 路径 |
|---|---|
| HamGNN 仓库 | /home/wangchaoyue852/software/Uni-HamGNN/HamGNN（已 setup.py 装进 ML 环境）|
| 通用模型 | /home/wangchaoyue852/software/Uni-HamGNN/uni-hamgnn_2_1.pkl（927 MB）|
| DFT_DATA19 | /home/wangchaoyue852/software/Uni-HamGNN/DFT_DATA19（取自 OpenMX 4.0 GPU 包）|
| Python 环境 | conda ML（Python 3.9，torch 1.11 / torch-geometric 2.0.4 / e3nn 0.5.0 / pymatgen）|
| openmx_postprocess / read_openmx | HamGNN/DFT_interfaces/openmx/openmx_postprocess/（GNU 编译）|
| 编译工具链 | conda openmx_build（gcc/gfortran 14 + OpenMPI + GSL + MKL）|

编译要点（Intel→GNU 移植，已踩平）：makefile 改为 mpicc/mpif90（conda openmx_build），
加 -fcommon -Wno-implicit-function-declaration -fallow-argument-mismatch，链接补
-lgfortran -lmpi_usempif08 -lmpi_usempi_ignore_tkr -lmpi_mpifh；MKL 用共享库
（libmkl_gnu_thread + libmkl_core + libmkl_intel_lp64 + libmkl_scalapack_lp64
+ libmkl_blacs_openmpi_lp64）。

## 关键参数（templates/step.conf，已按 3090 固化）

| 键 | 说明 |
|---|---|
| HAMGNN_DIR / UNI_MODEL / DFT_DATA | 见上表路径 |
| CONDA_SH / CONDA_ENV | 3090 = /home/wangchaoyue852/miniconda3/... + ML |
| SOC | true（默认）= 通用 SOC 模型（non-SOC + SOC 两份 graph_data）|
| NAO_MAX | OpenMX 最大轨道数 14/19/26（默认 26）|
| NPROC | ★ 3090 上固定 1：多进程 openmx_postprocess 卡在 MPI 收尾不退出 |
| MPIRUN | 完整路径 = openmx_build 的 mpirun（ML 环境无 MPI）|
| NTHREADS / DEVICE | predict 线程数 / 设备（3090 纯 CPU = cpu）|
| NK | band_cal 能带路径 k 点数（默认 120）|
| XC / ENERGY_CUTOFF / KGRID / SCF_CRITERION / MAX_SCF_ITER / ELECTRONIC_TEMP | OpenMX SCF 参数 |

## 测试

    tf skills                        # 应看到 unihamgnn，版本 0.1，3 步
    tf -tt unihamgnn list            # 列头 = S1_graph / S2_predict / S3_band
    cd <含 POSCAR 的材料上级目录>
    tf -tt unihamgnn init
    tf -tt unihamgnn -p <材料> -j 1 init     # 只生成输入、不提交，检查后再 start
    tf -tt unihamgnn -p <材料> start

## 注意

- 纯 CPU 跑：驱动里已 export CUDA_VISIBLE_DEVICES=""，torch 不碰 GPU。
- 3090 偶发 transient SIGTERM（约 50%，命中随机 Python 进程）：驱动/后处理已加自动重试
  （predict 3 次、band_cal 3 次），判据以产物文件为准（不是进程退出码）。
- 能带只对周期性体系（2D/3D）有定义，0D 孤立分子会在 step1 直接报错。
- 判据只读 json，不逐帧解析大矩阵，符合「数值重活作业算、判据只读 json」的约定。
