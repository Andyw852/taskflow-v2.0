#!/usr/bin/env bash
# sync_check.sh —— 对齐审计：diff v2.0 的 skill/ + setting/ 与「原版 taskflow/」。
# 用于防止"拷贝落后原版"类回归（如 stepconf.py 少了集群注入键）。
# 用法:
#   scripts/sync_check.sh                 # 对比默认原版 ~/software/taskflow
#   scripts/sync_check.sh /path/original  # 对比指定原版目录
# 退出码: 0=完全对齐；1=有差异（内容级差异，排除 __pycache__/运行时缓存）
set -uo pipefail

ORIG="${1:-/home/wangchao/software/taskflow}"
V2="$(cd "$(dirname "$0")/.." && pwd)"

EXC='__pycache__|\.pyc$|\.tf_(state_cache|hung|summary|watch)'

dirty=0
for sub in skill setting; do
  diff_out=$(diff -rq "$V2/$sub" "$ORIG/$sub" 2>/dev/null | grep -vE "$EXC")
  if [ -n "$diff_out" ]; then
    echo "[$sub/] 与原版不一致："
    echo "$diff_out" | sed 's/^/  /'
    dirty=1
  fi
done

if [ "$dirty" -eq 0 ]; then
  echo "OK：skill/ 与 setting/ 已与原版内容级对齐。"
  exit 0
fi

echo ""
echo "发现差异：用下面命令同步（checksum 模式，方向原版 → v2.0）："
echo "  rsync -ac --delete --exclude='__pycache__/' --exclude='*.pyc' '$ORIG/skill/' '$V2/skill/'"
echo "  rsync -ac --delete --exclude='__pycache__/' --exclude='*.pyc' --exclude='.tf_*' '$ORIG/setting/' '$V2/setting/'"
exit 1
