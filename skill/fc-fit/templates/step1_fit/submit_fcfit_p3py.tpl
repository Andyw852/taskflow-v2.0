#!/bin/bash
# fc-fit S1_fit job template -- FIT_ENGINE=phono3py (symfc / alm least squares).
# Placeholders: JOBNAME, CONDA_SH, CONDA_ENV, ENGINE.
# Resource overrides: put a [submit] section in step1_fit/step.conf, e.g.
#   [submit]
#   cpus_per_task = 64
#   qos           = premium
#   time          = 24:00:00
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
for _m in numpy scipy phonopy phono3py spglib h5py; do
    python -c "import ${_m}" 2>/dev/null && echo "  ok  ${_m}" \
        || echo "  MISSING ${_m} (required by FIT_ENGINE=phono3py)"
done
python -c "import symfc" 2>/dev/null && echo "  ok  symfc" \
    || echo "  MISSING symfc (needed when FC_CALC=symfc: pip install symfc)"
python -c "import hiphive" 2>/dev/null && echo "  ok  hiphive (ShengBTE export)" \
    || echo "  note: hiphive absent -- the ShengBTE export will be skipped"

set -e
# prep: normalise the dataset -> POSCAR/SPOSCAR/dataset_*.npy/disp_matrix.pkl
python fc_fit_driver.py prep fit_config.json
# fit: phono3py + symfc/alm -> fc2.hdf5 (+ fc3.hdf5)
python fc_fit_driver.py fit  fit_config.json
# post: ShengBTE export (optional) + imaginary-frequency gate -> fc_fit_summary.json
python fc_fit_driver.py post fit_config.json
