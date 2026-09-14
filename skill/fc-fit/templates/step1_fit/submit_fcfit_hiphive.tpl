#!/bin/bash
# fc-fit S1_fit job template -- FIT_ENGINE=hiphive (cluster space + regression).
# Placeholders: JOBNAME, CONDA_SH, CONDA_ENV, ENGINE.
# hiphive builds a (large) design matrix and solves a dense least-squares
# problem, so it wants cores and memory; override via step1_fit/step.conf:
#   [submit]
#   cpus_per_task = 64
#   mem           = 128G
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=48
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=regular
cd $SLURM_SUBMIT_DIR

if [ -n "{{CONDA_ENV}}" ] && [ -x "{{CONDA_ENV}}/bin/python" ]; then
    source "{{CONDA_ENV}}/bin/activate"
else
    source {{CONDA_SH}}
    conda activate {{CONDA_ENV}}
fi

NCPU=${SLURM_CPUS_PER_TASK:-48}
export OMP_NUM_THREADS=${NCPU}
export OPENBLAS_NUM_THREADS=${NCPU}
export MKL_NUM_THREADS=${NCPU}

echo "[env] host=$(hostname) env=${CONDA_DEFAULT_ENV:-${VIRTUAL_ENV:-}} threads=${NCPU}"
for _m in numpy scipy phonopy spglib h5py ase hiphive sklearn; do
    python -c "import ${_m}" 2>/dev/null && echo "  ok  ${_m}" \
        || echo "  MISSING ${_m} (required by FIT_ENGINE=hiphive)"
done
python -c "import numba" 2>/dev/null && echo "  ok  numba (hiphive force evaluation)" \
    || echo "  note: numba absent -- the fit-residual check will be skipped"

set -e
python fc_fit_driver.py prep fit_config.json
python fc_fit_driver.py fit  fit_config.json
python fc_fit_driver.py post fit_config.json
