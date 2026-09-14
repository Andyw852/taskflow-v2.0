#!/bin/bash
# fc-fit S1_fit job template -- FIT_ENGINE=pheasy with PHEASY_BIN=pheasy-gpu.
# Placeholders: JOBNAME, CONDA_SH, CONDA_ENV, ENGINE.
# GPU clusters need their own partition/gres; the values below match the local
# 3090/a800 boxes.  For a different cluster drop a copy of this file into
# setting/<hpc>/templates/submit_fcfit_pheasy_gpu.tpl (it takes priority).
#SBATCH --partition=gpu
#SBATCH --gres=gpu:2
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --output=queue.out
#SBATCH --error=queue.err
cd $SLURM_SUBMIT_DIR

if [ -n "{{CONDA_ENV}}" ] && [ -x "{{CONDA_ENV}}/bin/python" ]; then
    source "{{CONDA_ENV}}/bin/activate"
else
    source {{CONDA_SH}}
    conda activate {{CONDA_ENV}}
fi

export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}
export MKL_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}
export PHEASY_USE_GPU=1
export PHEASY_GPU_SM=1
export PHEASY_GPU_SM_NGPU=${PHEASY_GPU_SM_NGPU:-2}
export PHEASY_GPU_SM_DEVICES=${PHEASY_GPU_SM_DEVICES:-0,1}
export PHEASY_TWOLEVEL_CACHE_T=0

echo "[env] host=$(hostname) env=${CONDA_DEFAULT_ENV:-${VIRTUAL_ENV:-}} gpus=${PHEASY_GPU_SM_NGPU}"
for _m in numpy scipy phonopy spglib h5py; do
    python -c "import ${_m}" 2>/dev/null && echo "  ok  ${_m}" \
        || echo "  MISSING ${_m} (required by fc-fit)"
done
python -c "import pheasy_gpu" 2>/dev/null && echo "  ok  pheasy_gpu" \
    || echo "  MISSING pheasy_gpu (PHEASY_BIN=pheasy-gpu needs the GPU build)"
nvidia-smi -L 2>/dev/null || echo "  note: nvidia-smi unavailable"

set -e
python fc_fit_driver.py prep fit_config.json
python fc_fit_driver.py fit  fit_config.json
python fc_fit_driver.py post fit_config.json
