#!/usr/bin/env bash
# tf 脱敏提交推送技能（通用版）—— 在任意 git 仓库目录运行
# 用法: cd <任意项目> && git-sanitize-push "commit message"
#   或: bash ~/software/taskflow/scripts/tf-git-push.sh "message"
set -euo pipefail

# 解析脚本真实路径（支持软链调用）
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
MSG="${1:-update}"

# 敏感词映射：全局 ~/.config/git-sanitize.conf > taskflow 本地
CONF=""
for c in "$HOME/.config/git-sanitize.conf" "$SCRIPT_DIR/../setting/git-sanitize.conf"; do
  [ -f "$c" ] && { CONF="$c"; break; }
done
[ -n "$CONF" ] || { echo "缺敏感词映射：$HOME/.config/git-sanitize.conf"; exit 1; }

# 必须在 git 仓库内
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "请在 git 仓库目录里运行"; exit 1; }
git remote get-url origin >/dev/null 2>&1 || { echo "无 origin 远程"; exit 1; }

CUR="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"

# 确保 github SSH 走 443（绕过 22 端口封锁）
if ! grep -q 'ssh.github.com' "$HOME/.ssh/config" 2>/dev/null; then
  cat >> "$HOME/.ssh/config" <<'EOF'

Host github.com
    HostName ssh.github.com
    Port 443
    User git
    ConnectTimeout 10
    ServerAliveInterval 30
    ServerAliveCountMax 2
EOF
  echo "已配置 github SSH 走 443"
fi

# 1. 本地真实版提交（保留本地历史，不 push）
git add -A
git commit -m "${MSG}（本地真实版）" || echo "(无变更可提交)"

# 2. 基于远程建脱敏快照分支
git fetch origin
BASE=""
for b in "origin/$CUR" origin/main origin/master; do
  git rev-parse --verify "$b" >/dev/null 2>&1 && { BASE="$b"; break; }
done
[ -n "$BASE" ] || { echo "找不到远程基线分支"; exit 1; }
git checkout -B push-sanitized "$BASE"
git read-tree -u --reset "$CUR"
python3 "$SCRIPT_DIR/sanitize.py" "$CONF"
git add -A
git commit -m "${MSG}（脱敏版）"

# 3. push 重试（github 间歇性通，最多 8 次）
ok=0
for i in $(seq 1 8); do
  echo "push 第 ${i} 次..."
  if timeout 45 git push origin push-sanitized:"$CUR"; then ok=1; break; fi
  pkill -f git-upload-pack 2>/dev/null || true
  sleep 4
done

# 4. 清理：切回原分支、删临时分支、杀残留
git checkout "$CUR"
git branch -D push-sanitized 2>/dev/null || true
pkill -f git-upload-pack 2>/dev/null || true

if [ "$ok" = 1 ]; then echo "✅ push 完成"; else echo "❌ push 失败（8 次重试）"; exit 1; fi
