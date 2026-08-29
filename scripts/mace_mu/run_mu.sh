#!/usr/bin/env bash
# =============================================================================
# run_mu.sh —— 本地计算 38 种金属单质每原子能量（参考化学势 μ）
# 用法：bash run_mu.sh [模型路径]
#   - 模型缺省用 taskflow 技能自带副本：
#     skill/kl-mace-cpu/templates/mace/MACE-matpes-pbe-omat-ft.model
#   - 可用环境变量 MACE_MODEL_PATH 覆盖（如超算上的模型路径）
# 产物：本目录 results.json（逐元素 E_per_atom）+ 控制台打印 MU 单行
# =============================================================================
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HERE/venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "先运行 setup_local.sh 安装环境" >&2
  exit 1
fi

export MACE_MODEL_PATH="${1:-${MACE_MODEL_PATH:-}}"
cd "$HERE"
"$VENV/bin/python" calc_mu.py

echo
echo "===== 生成的 MU 单行（C 沿用石墨参考 -9.1757，粘到 step3_formation/step.conf）====="
python3 - <<'PY'
import json
d = json.load(open("results.json"))
mu = ["C:-9.1757"]
for el in ["Ag","Al","Au","Ba","Be","Ca","Cd","Co","Cr","Cs","Cu","Fe","Hf","Ir","K","Li","Mg","Mn","Mo","Na","Nb","Ni","Os","Pd","Pt","Rb","Re","Rh","Ru","Sc","Sr","Ta","Ti","V","W","Y","Zn","Zr"]:
    v = d.get(el) or {}
    if "E_per_atom" in v:
        mu.append("%s:%.5f" % (el, v["E_per_atom"]))
    else:
        print("警告：%s 未算成（%s）" % (el, v.get("error", "?")), file=__import__("sys").stderr)
print("MU = " + " ".join(mu))
PY
