#!/bin/bash
# S5_fc 拟合作业模板（FIT_ENGINE=phono3py，symfc/alm）。占位符 {{JOBNAME}}
# 资源可用 step.conf 的 [submit] 段覆盖：cpus_per_task / qos / partition / time。
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=48
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=premium
cd $SLURM_SUBMIT_DIR
source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh
conda activate atomate2_p_a

NCPU=${SLURM_CPUS_PER_TASK:-48}
export OMP_NUM_THREADS=${NCPU}
export OPENBLAS_NUM_THREADS=${NCPU}
export MKL_NUM_THREADS=${NCPU}

echo "[env] $CONDA_DEFAULT_ENV | phono3py=$(python -c 'import phono3py;print(phono3py.__version__)' 2>&1)"
python -c "import symfc" 2>/dev/null && echo "[env] symfc OK" || echo "[env] ⚠️ 无 symfc（FC_CALC=symfc 时必需：pip install symfc）"

set -e
# prep：收力 → FORCES_FC3 → POSCAR/SPOSCAR（alm 再落 npy）→ BORN
python kl_fc_backends.py prep         fit_config.json
# 拟合：phono3py + symfc/alm → phono3py/{fc2,fc3}.hdf5 (+ phono3py_params.yaml)
python kl_fc_backends.py fit_phono3py fit_config.json
# post：shengbte 力常数导出（可选）+ 虚频闸 → phonon_summary.json
python kl_fc_backends.py post         fit_config.json
