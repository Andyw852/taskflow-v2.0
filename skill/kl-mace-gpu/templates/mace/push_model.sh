#!/usr/bin/env bash
# push_model.sh —— 把本目录的 .model 一次性推到超算的模型库目录。
#
#   bash push_model.sh [ssh别名] [远端目录] [只推某个文件]
#   bash push_model.sh jzzn /public/home/wangchao/software/mace_models
#   bash push_model.sh jzzn /public/home/wangchao/software/mace_models my.model
#
# 推完把远端目录填进 kl-mace-gpu 的全局 step.conf：
#   tf -tt kl-mace-gpu -p <材料> conf --set params.MACE_MODEL_DIR=<远端目录>
set -euo pipefail

HOST="${1:-jzzn}"
DEST="${2:-/public/home/wangchao/software/mace_models}"
ONLY="${3:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

shopt -s nullglob
if [[ -n "$ONLY" ]]; then
  FILES=("$HERE/$ONLY")
  [[ -f "${FILES[0]}" ]] || { echo "[ERROR] 找不到 $HERE/$ONLY" >&2; exit 1; }
else
  FILES=("$HERE"/*.model "$HERE"/*.pt)
fi
if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "[ERROR] $HERE 下没有 .model / .pt 文件。先把模型放进来。" >&2
  exit 1
fi

echo "[..] 目标 $HOST:$DEST"
ssh "$HOST" "mkdir -p '$DEST'"

for f in "${FILES[@]}"; do
  echo "[..] $(basename "$f")  $(du -h "$f" | cut -f1)"
  if command -v rsync >/dev/null 2>&1; then
    rsync -avP "$f" "$HOST:$DEST/"
  else
    scp "$f" "$HOST:$DEST/"
  fi
done

echo "[OK] 远端现有模型："
ssh "$HOST" "ls -lh '$DEST'"
echo
echo "接着设："
echo "  tf -tt kl-mace-gpu -p <材料> conf --set params.MACE_MODEL_DIR=$DEST"
echo "  tf -tt kl-mace-gpu -p <材料> conf --set params.MACE_MODEL=$(basename "${FILES[0]}")"
