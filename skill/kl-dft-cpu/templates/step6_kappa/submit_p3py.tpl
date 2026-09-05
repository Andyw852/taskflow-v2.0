#!/bin/bash
# phono3py BTE 提交模板（step6_kappa, solver=phono3py）。占位符 {{JOBNAME}} {{P3PY_CMD}}
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=48
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=premium
cd $SLURM_SUBMIT_DIR
source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh
conda activate atomate2_p_a
export OMP_NUM_THREADS=$SLURM_NTASKS_PER_NODE
echo "[phono3py] env=$CONDA_DEFAULT_ENV which=$(which phono3py)"; phono3py --version || true
{{P3PY_CMD}}
