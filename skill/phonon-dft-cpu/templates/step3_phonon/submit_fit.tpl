#!/bin/bash
# phonon-dft-cpu S3 拟合作业模板（phonopy 收力 + fc2 + 声子谱）。占位符 JOBNAME
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=regular
cd $SLURM_SUBMIT_DIR
source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh
conda activate atomate2_p_a
NCPU=8
export OMP_NUM_THREADS=$NCPU
export OPENBLAS_NUM_THREADS=$NCPU
export MKL_NUM_THREADS=$NCPU
echo "[env] $CONDA_DEFAULT_ENV | phonopy=$(python -c 'import phonopy;print(phonopy.__version__)' 2>&1)"
python phonon_fit_driver.py 2>&1 | tee -a phonon_fit.log
