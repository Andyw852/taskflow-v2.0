#!/bin/bash
# S5_fc 拟合作业模板（FIT_ENGINE=pheasy）。移植自用户 pheasy 拟合脚本，去掉 VCA/内存监控/
# 尾部自动提交 κ；输入准备(prep)与收尾(collect/post)交给 kl_fc_backends.py。
# 占位符：{{JOBNAME}} {{DIM}} {{FIT_METHOD}} {{ENABLE_FC}} {{C3_CUTOFF}} {{NULL_SPACE_EPS}}
# 资源（cpus_per_task/qos）建议按体系用 step.conf 的 [submit] 段覆盖；pheasy 较吃核与内存。
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
set -e

# ===== 环境自检 =====
for _m in numpy scipy phonopy spglib phono3py pheasy; do
    python -c "import ${_m}" 2>/dev/null && echo "  ✅ ${_m}" \
        || { echo "  ❌ ${_m} 导入失败，检查 atomate2_p_a 环境"; exit 1; }
done

# ===== 用户参数（gen 从 step.conf 注入）=====
DIM="{{DIM}}"                 # 超胞对角三整数
FIT_METHOD="{{FIT_METHOD}}"   # LASSO | RFE | OLS
ENABLE_FC={{ENABLE_FC}}       # 2|3|4
C3_CUTOFF="{{C3_CUTOFF}}"     # fc3 截断 Å，None=不截断
NULL_SPACE_EPS={{NULL_SPACE_EPS}}
FIT_ORDER=${ENABLE_FC}

# ===== 并行 =====
NCPU=${SLURM_CPUS_PER_TASK:-48}
NCPU_BLAS=$(( NCPU>32 ? 32 : NCPU ))
NCPU_DISP=${NCPU}
if [ "${FIT_METHOD}" = "RFE" ] || [ "${FIT_METHOD}" = "OLS" ]; then
    NCPU_LOKY=1; NCPU_FIT_BLAS=${NCPU}
else
    NCPU_LOKY=1; NCPU_FIT_BLAS=${NCPU}
fi
[[ "${FIT_METHOD}" =~ ^(LASSO|RFE|OLS)$ ]] || { echo "❌ FIT_METHOD 非法: ${FIT_METHOD}"; exit 1; }
[[ "${ENABLE_FC}" =~ ^[234]$ ]] || { echo "❌ ENABLE_FC 非法: ${ENABLE_FC}"; exit 1; }

# ===== 输入准备：vasprun → FORCES_FC3 → POSCAR/SPOSCAR/dataset_*.npy =====
python kl_fc_backends.py prep fit_config.json
for f in POSCAR SPOSCAR dataset_disps.npy dataset_forces.npy; do
    [ ! -f "$f" ] && echo "❌ prep 后仍缺 $f" && exit 1
done

# ===== 加速 / BLAS 线程 =====
export PHEASY_SVD_THRESHOLD=500
export PHEASY_ASR_COMBINED=1
export PHEASY_ASR_LWORK_LIMIT=1500000000
export PHEASY_SM_DTYPE=float32
export PHEASY_SM_THR=1e-12
export PHEASY_ASR_SPARSE=1
export PHEASY_ASR_SPARSE_THR=1e-10
export PHEASY_ASR_COL_BLOCK=5000
export OPENBLAS_NUM_THREADS=${NCPU_BLAS}
export OMP_NUM_THREADS=${NCPU_BLAS}
export MKL_NUM_THREADS=${NCPU_BLAS}
export PHEASY_BLAS_THREADS=${NCPU_BLAS}
export PHEASY_USE_CELER=1

# RFE 触发（复用用户脚本的 PHEASY_USE_RFE 重定向）
if [ "${FIT_METHOD}" = "RFE" ]; then
    export PHEASY_USE_RFE=1 PHEASY_RFE_TWOLEVEL=1 MKL_INTERFACE_LAYER=ILP64 PHEASY_RFE_MKL=1
    export PHEASY_RFE_STEP=0.1 PHEASY_RFE_RIDGE_ALPHA=1e-11 PHEASY_RFE_CV=5
    export PHEASY_RFE_LSMR_MAXITER=60000 PHEASY_RFE_WARM_START=1
    export PHEASY_COLNORM_FRAMES=24 PHEASY_COLNORM_EXACT=0 PHEASY_RFE_ONE_SE=1
    echo "RFE 启用：step=0.1 ridge=1e-11 cv=5"
elif [ "${FIT_METHOD}" = "OLS" ]; then
    export MKL_INTERFACE_LAYER=ILP64 PHEASY_OLS_TWOLEVEL=1 PHEASY_RFE_MKL=1
    export PHEASY_OLS_MAXITER=500 PHEASY_OLS_RIDGE=1e-4 PHEASY_OLS_ATOL=1e-6 PHEASY_OLS_BTOL=1e-6
    echo "OLS 启用：两级 matvec, MKL ILP64（内存较高）"
else
    python -c "from celer import Lasso" 2>/dev/null || { echo "❌ LASSO 需 celer，禁止提交"; exit 1; }
fi

# ===== 数据处理：dataset_*.npy → disp_matrix.pkl/force_matrix.pkl + ndata =====
python << 'PYEOF'
import numpy as np, pickle
from phonopy.interface.vasp import read_vasp
sup = read_vasp('SPOSCAR'); natom = len(sup.numbers)
d = np.load('dataset_disps.npy'); f = np.load('dataset_forces.npy')
eq = d[-1]
# prep 写的是笛卡尔位移 + 末帧零平衡帧；这里统一扣末帧还原（幂等）
is_frac = (d.min() >= -0.15 and d.max() <= 1.15 and 0.2 < d.mean() < 0.8)
if is_frac:
    dd = d - eq; dd = np.where(dd > 0.5, dd-1, np.where(dd < -0.5, dd+1, dd))
    dcart = np.einsum('ij,njk->nik', sup.cell.T, np.transpose(dd,(0,2,1)))
    dcart = np.transpose(dcart,(0,2,1))
else:
    dcart = d
disps = (dcart - dcart[-1])[:-1]
forces = (f - f[-1])[:-1]
rms = np.sqrt((disps**2).mean())
assert 1e-6 < rms < 1.0, "RMS位移异常 %.3e" % rms
print("参与拟合 %d 帧, RMS位移=%.4f Å, natom_super=%d" % (len(disps), rms, natom))
# 照 doc2 落全 4 个文件：pheasy --disp_file 依赖 pkl；npy 供其它读取路径/对拍，一并写全以防口径不一致
np.save('dataset_disps_cartesian.npy', disps)
np.save('dataset_forces_corrected.npy', forces)
pickle.dump(disps,  open('disp_matrix.pkl','wb'))
pickle.dump(forces, open('force_matrix.pkl','wb'))
open('ndata_total.txt','w').write(str(len(disps)))
open('natom_super.txt','w').write(str(natom))
PYEOF
NDATA=$(cat ndata_total.txt)
export PHEASY_CV_GROUP_SIZE=$(( 3 * $(cat natom_super.txt) ))
echo "ndata=${NDATA}  CV_GROUP_SIZE=${PHEASY_CV_GROUP_SIZE}"

# ===== 参数拼装 =====
C_FLAG=""
[ "${C3_CUTOFF}" != "None" ] && [ "${C3_CUTOFF}" != "none" ] && [ -n "${C3_CUTOFF}" ] \
    && [ "${FIT_ORDER}" -ge 3 ] && C_FLAG="--c3 ${C3_CUTOFF}"
W_FLAG="-w ${FIT_ORDER}"
CLI_METHOD="${FIT_METHOD}"; [ "${FIT_METHOD}" = "RFE" ] && CLI_METHOD="LASSO"   # RFE 走 env 重定向
FIT_FLAGS="--full_ifc -l ${CLI_METHOD} --rasr BHH --hdf5"
if [ "${FIT_METHOD}" = "LASSO" ]; then
    if [ "${FIT_ORDER}" -eq 2 ]; then FIT_FLAGS="${FIT_FLAGS} --mu_min -8 --mu_max 0 --max_iter 100000"
    else FIT_FLAGS="${FIT_FLAGS} --mu_min -8 --mu_max -5 --max_iter 100000"; fi
    FIT_FLAGS="${FIT_FLAGS} --cv 5 --nmu 40 --tol 0.00001"
elif [ "${FIT_METHOD}" = "RFE" ]; then
    FIT_FLAGS="${FIT_FLAGS} --mu_min -8 --mu_max -5 --max_iter 1000 --cv 5 --nmu 5 --tol 0.001"
fi

# ===== pheasy 四步：cluster space → 对称约束 → 位移矩阵 → 拟合 =====
echo "【pheasy】阶次=${FIT_ORDER} 方法=${FIT_METHOD} ndata=${NDATA} C_FLAG='${C_FLAG}'"
rm -f fc2.hdf5 fc3.hdf5 fc4.hdf5

export OPENBLAS_NUM_THREADS=${NCPU_BLAS} OMP_NUM_THREADS=${NCPU_BLAS} MKL_NUM_THREADS=${NCPU_BLAS}
pheasy --dim ${DIM} ${W_FLAG} -s ${C_FLAG} --eps ${NULL_SPACE_EPS}
pheasy --dim ${DIM} ${W_FLAG} -c ${C_FLAG} --eps ${NULL_SPACE_EPS}

export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PHEASY_N_JOBS=${NCPU_DISP}
pheasy --dim ${DIM} ${W_FLAG} -d ${C_FLAG} --ndata ${NDATA} --disp_file --eps ${NULL_SPACE_EPS}

export LOKY_MAX_CPU_COUNT=${NCPU_LOKY} OPENBLAS_NUM_THREADS=${NCPU_FIT_BLAS}
export OMP_NUM_THREADS=${NCPU_FIT_BLAS} MKL_NUM_THREADS=${NCPU_FIT_BLAS}
export PHEASY_N_JOBS=${NCPU_LOKY} PHEASY_DOT_THREADS=${NCPU} OMP_NESTED=FALSE MKL_DYNAMIC=FALSE
pheasy --dim ${DIM} ${W_FLAG} -f ${C_FLAG} --ndata ${NDATA} --eps ${NULL_SPACE_EPS} ${FIT_FLAGS}

# ===== 输出检查 =====
sync
[ ! -f fc2.hdf5 ] && echo "❌ 缺 fc2.hdf5" && exit 1
[ "${FIT_ORDER}" -ge 3 ] && [ ! -f fc3.hdf5 ] && echo "❌ 缺 fc3.hdf5" && exit 1
ls -lh fc2.hdf5 fc3.hdf5 2>/dev/null

# ===== 收尾：搬产物 + shengbte 导出 + 虚频闸 =====
python kl_fc_backends.py collect_pheasy fit_config.json
python kl_fc_backends.py post           fit_config.json
