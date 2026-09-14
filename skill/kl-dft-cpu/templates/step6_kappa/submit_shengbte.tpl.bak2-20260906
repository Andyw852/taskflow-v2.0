#!/bin/bash
# ShengBTE BTE 提交模板（step6_kappa, SOLVER=shengbte）。占位符 {{JOBNAME}} {{SHENGBTE_EXE}}
# 输入 CONTROL + FORCE_CONSTANTS_2ND/3RD 已由 gen_step6 备好（力常数拷自 S5_fc/shengbte/）。
#SBATCH --partition=cpu192
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=48
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=premium
module purge
module load gcc/14.1
module load openmpi/4.0.1
source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh
conda activate atomate2_p_a
cd $SLURM_SUBMIT_DIR

for f in CONTROL FORCE_CONSTANTS_2ND FORCE_CONSTANTS_3RD; do
    [ ! -f "$f" ] && echo "❌ 缺 $f" >&2 && exit 1
done

export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
export OMP_PROC_BIND=close OMP_PLACES=cores OMP_NESTED=FALSE
export BLIS_NUM_THREADS=$OMP_NUM_THREADS
export AOCL_ENABLE_INSTRUCTIONS=AVX512
export LD_LIBRARY_PATH=/public/home/wangchao/software/aocl-gcc/5.0.0/gcc/lib_LP64:$CONDA_PREFIX/lib:$LD_LIBRARY_PATH
unset MKL_NUM_THREADS MKL_DEBUG_CPU_TYPE I_MPI_PMI_LIBRARY
ulimit -s unlimited

mpirun -n 1 \
    --bind-to core \
    --mca pml ucx --mca osc ucx --mca btl ^openib,tcp \
    -x UCX_TLS=rc,sm,self -x UCX_NET_DEVICES=mlx5_0:1 -x UCX_LOG_LEVEL=error \
    -x OMP_NUM_THREADS -x OMP_PROC_BIND -x OMP_PLACES \
    -x BLIS_NUM_THREADS -x AOCL_ENABLE_INSTRUCTIONS -x LD_LIBRARY_PATH \
    {{SHENGBTE_EXE}} > shengbte.log 2>&1

# 汇总：优先 _CONV（迭代解），退回 _RTA
python - <<'PY'
import glob, json
cand = ["BTE.KappaTensorVsT_CONV", "BTE.KappaTensorVsT_RTA"]
f = next((c for c in cand if glob.glob(c)), None)
d = {"KAPPA_DONE": bool(f)}
if f:
    rows = [l.split() for l in open(f) if l.strip() and not l.startswith("#")]
    if rows:
        d["source"] = f
        d["temperatures"] = [float(r[0]) for r in rows]
        # ShengBTE KappaTensorVsT：col0=T，col1..9=kappa 张量 xx xy xz yx yy yz zx zy zz
        d["kappa_xx_yy_zz"] = [[float(r[1]), float(r[5]), float(r[9])] for r in rows]
    else:
        d["KAPPA_DONE"] = False
json.dump(d, open("kappa_summary.json", "w"), ensure_ascii=False, indent=2)
print("KAPPA_DONE" if d["KAPPA_DONE"] else "NO_KAPPA")
PY
