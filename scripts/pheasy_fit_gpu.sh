#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
用法：bash scripts/pheasy_fit_gpu.sh [METHOD] [C2_CUTOFF] [C3_CUTOFF] [NDATA] [NGPU]

METHOD      OLS 或 LASSO，默认 LASSO
C2_CUTOFF  二阶截断半径，默认 6.0 Å
C3_CUTOFF  三阶截断；二阶拟合使用 None，默认 None
NDATA       使用的构型数；默认使用全部构型
NGPU        使用卡数，默认 1；必须不超过调度器分配的可见 GPU 数

目录必须包含 POSCAR、SPOSCAR、disp_matrix.pkl、force_matrix.pkl。
沿用 pheasy_fit.sh 自动识别超胞并构建缓存；求解参数采用固定基线。
默认使用可见 GPU 的稀疏乘法，设备分配由调度环境决定。
本脚本不申请 GPU；提交时须另设 --gres=gpu:N，并确保 CUDA_VISIBLE_DEVICES 正确。
例：bash scripts/pheasy_fit_gpu.sh LASSO 6.0 None "" 2
EOF
}

[[ "${1:-}" != "-h" && "${1:-}" != "--help" ]] || { usage; exit 0; }
method="${1:-LASSO}"
[[ $# -le 5 ]] || { usage >&2; exit 2; }
c2="${2:-6.0}"
c3="${3:-None}"
ndata="${4:-}"
ngpu="${5:-1}"
[[ "$ngpu" =~ ^[1-9][0-9]*$ ]] || { echo "NGPU 必须为正整数" >&2; exit 2; }
threads="${SLURM_CPUS_PER_TASK:-8}"
[[ "$threads" =~ ^[1-9][0-9]*$ ]] || { echo "SLURM_CPUS_PER_TASK 必须为正整数" >&2; exit 2; }
case "$method" in OLS|LASSO) ;; *) echo "METHOD 只能是 OLS 或 LASSO" >&2; exit 2 ;; esac
[[ "$c2" =~ ^[0-9]+([.][0-9]+)?$ && "$c2" =~ [1-9] ]] || { echo "二阶截断必须为正数" >&2; exit 2; }
[[ "$c3" == None || ( "$c3" =~ ^[0-9]+([.][0-9]+)?$ && "$c3" =~ [1-9] ) ]] || { echo "三阶截断必须为正数或 None" >&2; exit 2; }
[[ -z "$ndata" || "$ndata" =~ ^[1-9][0-9]*$ ]] || { echo "构型数必须为正整数" >&2; exit 2; }

root="${PHEASY_ROOT:-$HOME/software/pheasy-gpu}"
bin="${PHEASY_BIN:-pheasy-gpu}"
command -v "$bin" >/dev/null || { echo "找不到 $bin，请设置 PHEASY_BIN" >&2; exit 2; }
[[ -f "$root/pheasy_fit.sh" ]] || { echo "找不到 $root/pheasy_fit.sh，请设置 PHEASY_ROOT" >&2; exit 2; }
for input in POSCAR SPOSCAR disp_matrix.pkl force_matrix.pkl; do
  [[ -f "$input" ]] || { echo "缺少 $input" >&2; exit 2; }
done
visible_gpus=$(python3 -c 'import torch; print(torch.cuda.device_count())')
[[ "$ngpu" -le "$visible_gpus" ]] || { echo "请求 $ngpu 张卡，但当前仅可见 $visible_gpus 张；请核对作业分配和 CUDA_VISIBLE_DEVICES" >&2; exit 2; }
devices=$(seq -s, 0 "$((ngpu - 1))")

export PHEASY_USE_GPU=1 PHEASY_GPU_LASSO=1
export PHEASY_GPU_SM=1
export PHEASY_GPU_SM_NGPU="$ngpu" PHEASY_GPU_SM_DEVICES="$devices"
export PHEASY_OLS_TWOLEVEL=1
export PHEASY_TWOLEVEL_CACHE_T=0 PHEASY_LASSO_SPARSE=1 PHEASY_LASSO_TWOLEVEL=1
export PHEASY_SM_DTYPE=float32 PHEASY_N_JOBS="$threads"
export OPENBLAS_NUM_THREADS="$threads" OMP_NUM_THREADS="$threads" MKL_NUM_THREADS="$threads"
export PHEASY_EXECUTABLE="$bin"
order=2
[[ "$c3" == None ]] || order=3
exec bash "$root/pheasy_fit.sh" "FIT_METHOD=$method" "FIT_ORDER=$order" \
  "C2_CUTOFF=$c2" "C3_CUTOFF=$c3" "NDATA=$ndata" "NCPU=$threads" \
  STANDARDIZE=true CV=5 NMU=20 LASSO_TOL=1e-6 LASSO_MAX_ITER=20000 \
  SM_DTYPE=float32 LASSO_SPARSE=1 LASSO_TWOLEVEL=1
