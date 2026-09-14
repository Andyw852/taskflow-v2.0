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
def _read_kt(fname):
    rows = [l.split() for l in open(fname) if l.strip() and not l.startswith("#")]
    out = []
    for r in rows:
        try:
            vals = [float(x) for x in r[:10]]
        except ValueError:
            continue
        if abs(vals[1]) < 1e6 and abs(vals[5]) < 1e6 and abs(vals[9]) < 1e6:
            out.append(vals)
    return out

cand = ["BTE.KappaTensorVsT_CONV", "BTE.KappaTensorVsT_RTA"]
best = None
for c in cand:
    rows = _read_kt(c) if glob.glob(c) else []
    if not rows:
        continue
    if len(rows) >= 3:  # CONV 发散（1e147 量级）被过滤后可能 <3 行 -> 回退 RTA
        best = (c, rows)
        if c.endswith("_CONV"):
            # CONV 正常温度点数（CONTROL T 扫描计数）不足时：发散点被过滤 =
            # 曲线不完整，回退 RTA（更稳定、全温度）
            _tmax = None
            try:
                for ln in open("CONTROL"):
                    s = ln.strip()
                    if s.startswith("T_max="):
                        _tmax = float(s.split("=")[1].split(",")[0].strip())
                    elif s.startswith("T_min="):
                        pass
            except Exception:
                pass
            _nt_expected = 8
            if _tmax:
                try:
                    for ln in open("CONTROL"):
                        s = ln.strip()
                        if s.startswith("T_step="):
                            _ts = float(s.split("=")[1].split(",")[0].strip())
                            _nt_expected = int(round((_tmax - 100.0) / _ts)) + 1
                            break
                except Exception:
                    pass
            if len(rows) < _nt_expected:
                print("[shengbte] CONV 温度点 %d < 预期 %d（发散被过滤），回退 RTA" % (len(rows), _nt_expected))
                continue
        break
f, rows = best if best else (None, [])
d = {"KAPPA_DONE": bool(f)}
if f:
    d["source"] = f
    d["temperatures"] = [r[0] for r in rows]
    # ShengBTE KappaTensorVsT：col0=T，col1..9=kappa 张量 xx xy xz yx yy yz zx zy zz
    d["kappa_xx_yy_zz"] = [[r[1], r[5], r[9]] for r in rows]
json.dump(d, open("kappa_summary.json", "w"), ensure_ascii=False, indent=2)
print("KAPPA_DONE" if d["KAPPA_DONE"] else "NO_KAPPA")
PY
