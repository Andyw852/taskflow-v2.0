#!/bin/bash
# fourphonon(ShengBTE 同源, 3ph RTA) 提交模板 —— step6_kappa SOLVER=fourphonon
# 占位符: {{JOBNAME}} {{FOURPHONON_EXE}} {{FOURPHONON_NGPU}} {{FOURPHONON_CPUS_PER_GPU}}
# 输入 CONTROL + FORCE_CONSTANTS_2ND/3RD 由 gen_step6 备好(力常数拷自 S5_fc/shengbte/)。
#
# fourphonon GPU 版要点(2026-09 实战验证, 见 kl-dft-cpu/README.md):
#   1. 必须 multi-GPU 修复版(acc_set_device_num 自动选卡 + MPI_IN_PLACE 归约);
#      旧单 rank 版枚举段极慢(GPU 永不点火)。
#   2. rank 数 = GPU 数, 不要超过机器卡数。
#   3. Intel 2019 mpiexec.hydra 部分机器损坏(连 hostname 都 FPE) -> 用 NVHPC OpenMPI。
#   4. AOCC/flang 编译版大胞枚举会卡死(12h 零进展), 勿用于 >20 原子体系。
#SBATCH --partition=gpu
#SBATCH --job-name={{JOBNAME}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node={{FOURPHONON_NGPU}}
#SBATCH --cpus-per-task={{FOURPHONON_CPUS_PER_GPU}}
#SBATCH --gres=gpu:{{FOURPHONON_NGPU}}
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=normal
#SBATCH --time=48:00:00
cd $SLURM_SUBMIT_DIR

for f in CONTROL FORCE_CONSTANTS_2ND FORCE_CONSTANTS_3RD; do
    [ ! -f "$f" ] && echo "missing $f" >&2 && exit 1
done

# ---- NVHPC OpenMPI + CUDA + spglib/openblas(按机器改路径) ----
NVHPC=\${NVHPC_ROOT:-/opt/nvhpc/24.11/Linux_x86_64/24.11}
OMPI=$NVHPC/comm_libs/12.6/openmpi4/openmpi-4.1.5
CUDA=\${CUDA_ROOT:-/usr/local/cuda}
SPG=\${SPGLIB_DIR:-/path/to/spglib/lib}
BLAS=\${BLAS_DIR:-/path/to/openblas/lib}
export PATH=$OMPI/bin:$CUDA/bin:$PATH
export LD_LIBRARY_PATH=$SPG:$BLAS:$NVHPC/compilers/lib:$OMPI/lib:$NVHPC/math_libs/12.6/lib64:$CUDA/lib64:$LD_LIBRARY_PATH

export OMP_NUM_THREADS={{FOURPHONON_CPUS_PER_GPU}}
export OMP_PROC_BIND=spread OMP_PLACES=threads
export OMP_STACKSIZE=1G
ulimit -s unlimited

echo "fourphonon {{FOURPHONON_NGPU}}-GPU start: $(date)"

# rank 数 = GPU 数; 程序内 acc_set_device_num(myid) 自动选卡
# 每 rank 绑 CPUS_PER_GPU 个核(map-by numa:PE)供枚举段 OMP 使用
mpirun -n {{FOURPHONON_NGPU}} --map-by numa:PE={{FOURPHONON_CPUS_PER_GPU}} \
    --bind-to core \
    {{FOURPHONON_EXE}} > fourphonon.log 2>&1
echo "EXIT=$? end: $(date)"

# 汇总 RTA 结果 -> kappa_summary.json
python - <<'PY'
import glob, json
def _read_kt(fname):
    rows = []
    for l in open(fname):
        l = l.strip()
        if not l or l.startswith('#'):
            continue
        parts = l.split()
        if len(parts) < 10:
            continue
        try:
            vals = [float(x) for x in parts[:10]]
        except ValueError:
            continue
        if abs(vals[1]) < 1e6 and abs(vals[5]) < 1e6 and abs(vals[9]) < 1e6:
            rows.append(vals)
    return rows

rows = _read_kt('BTE.KappaTensorVsT_RTA') if glob.glob('BTE.KappaTensorVsT_RTA') else []
d = {'KAPPA_DONE': bool(rows), 'source': 'BTE.KappaTensorVsT_RTA' if rows else None}
if rows:
    d['temperatures'] = [r[0] for r in rows]
    d['kappa_xx_yy_zz'] = [[r[1], r[5], r[9]] for r in rows]
json.dump(d, open('kappa_summary.json', 'w'), ensure_ascii=False, indent=2)
print('KAPPA_DONE' if d['KAPPA_DONE'] else 'NO_KAPPA')
PY
