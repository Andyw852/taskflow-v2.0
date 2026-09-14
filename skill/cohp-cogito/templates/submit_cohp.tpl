#!/bin/bash
# =====================================================================
# submit_cohp.tpl —— COGITO 成键分析的提交模板
#
# 与 submit_std_*.tpl 的区别：不跑 mpirun vasp，而是在 conda 环境里跑
# COGITO 三件套（COGITO -> COGITOanalyze -> COGITOpost）。
#
# 为什么必须走 Slurm 而不能在登录节点跑：
#   tf 的 run:gen 本地步远端执行上限是 `timeout 600`（10 分钟），
#   而 COGITO 对 9 原子胞约需 23 分钟（Wannier 轨道生成为主）。
#
# ★ 结构约束：所有 #SBATCH 必须位于文件最前部、且在第一个非注释命令之前；
#   所以命令占位符只能放在最后（本文件末尾）。
#
# 集群参数写死在本文件；换机器/换队列只改这里。
# gen 脚本负责替换两处占位符（作业名 + 命令块）。
# =====================================================================
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=4
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=regular
# ★ 必须显式给 --time：jzzn 不写时默认只有 1 分钟（实测 TIMEOUT 00:01:00），
#   而 COGITO 对 9 原子胞约需 23 分钟。留足余量防大胞超时。
#SBATCH --time=04:00:00

cd $SLURM_SUBMIT_DIR || exit 1

# --- conda 环境（非交互 shell 必须先 source conda.sh）-----------------
source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh
conda activate atomate2_p_a

# 防止 BLAS 线程与 SLURM 分配打架；COGITO 本身单核为主
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

echo "[cohp] host=$(hostname) env=$CONDA_DEFAULT_ENV time=$(date '+%F %T')"
echo "[cohp] COGITO=$(command -v COGITO)"

# =====================================================================
# 本步骤要执行的 COGITO 命令（由 gen 脚本填充；必须放在 #SBATCH 之后）
# =====================================================================
{{COHP_CMD}}

echo "[cohp] all done rc=$? time=$(date '+%F %T')"
ls -l bond_info.txt metadata.json 2>/dev/null
