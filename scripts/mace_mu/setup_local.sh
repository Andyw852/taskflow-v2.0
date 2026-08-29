#!/usr/bin/env bash
# =============================================================================
# setup_local.sh —— 本地 MACE μ 计算环境一键安装（只用一次）
# 创建独立 venv（torch CPU + mace-torch + ase），不污染其它环境。
# 用法：bash setup_local.sh
# =============================================================================
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/venv"

if [ -x "$VENV/bin/python" ]; then
  echo "venv 已存在：$VENV（跳过安装）"
else
  python3 -m venv "$VENV"
  echo "[1/2] 安装 torch (CPU)..."
  "$VENV/bin/pip" install --quiet torch --index-url https://download.pytorch.org/whl/cpu
  echo "[2/2] 安装 mace-torch + ase..."
  "$VENV/bin/pip" install --quiet mace-torch ase
fi

"$VENV/bin/python" -c "import torch, mace, ase; print('OK  torch', torch.__version__, '| mace', mace.__version__, '| ase', ase.__version__)"
echo "环境就绪：$VENV"
