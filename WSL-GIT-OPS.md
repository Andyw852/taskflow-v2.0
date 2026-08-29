# WSL + dsh web 稳定性 + Git 提交 运维备忘

> 本机（WSL2 / wangchao@DESKTOP-3B6UKIT）的 dsh web 托管与 git 提交关键配置、踩坑记录。改配置或遇到卡顿/提交失败时先查这里。

## 1. dsh web 由 systemd 托管（崩溃自愈）

- 服务文件：`/etc/systemd/system/dsh-web.service`
- 关键配置（缺一不可）：
  - `Environment=NODE_OPTIONS=--max-old-space-size=8192` —— 防 V8 堆 OOM（大会话反序列化会撑爆默认 2GB 堆）
  - `MemoryMax=12G`、`Nice=5`、`Restart=always`、`RestartSec=5`
- 崩溃根因历史：`FATAL ERROR: JavaScript heap out of memory`（ValueDeserializer 反序列化大消息）→ 调大堆解决
- 运维命令：
  - `systemctl status dsh-web` / `sudo systemctl restart dsh-web`
  - 看崩溃日志：`journalctl -u dsh-web -f`

## 2. WSL 资源配置

- 文件：`C:\Users\wangchao\.wslconfig`
- 值：`memory=56GB`、`processors=12`、`autoMemoryReclaim=gradual`、`swap=12GB`
- 改完必须 `wsl --shutdown` 重启才生效
- 注意：56GB 让 Windows 只剩 8GB，页面文件需系统托管兜底

## 3. github SSH 走 443（校园网封锁 22/443 的解法）

- `~/.ssh/config` 的 github 段：`HostName ssh.github.com` + `Port 443`
- **用账号级 SSH key**（不是 deploy key）：`~/.ssh/id_ed25519`
  - 账号级认证返回 `Hi Andyw852!`
  - deploy key 返回 `Hi Andyw852/taskflow!`，且只能推单个仓库 → 其他仓库会 `denied to deploy key`
- 验证：`ssh -T git@github.com` 应返回 `Hi Andyw852!`
- https 协议仓库推不动（github.com:443 被墙，报 `Empty reply from server`）→ 改 ssh：
  `git remote set-url origin git@github.com:Andyw852/xxx.git`

## 4. git fetch 拦截器（防 dsh web 卡死）

- 文件：`~/.dsh/git-shim/git`（对 `git fetch` 加 `timeout 20`，其余原样转发）
- 已注入 `dsh-web.service` 的 PATH 最前
- 作用：dsh web 会反复 fetch workspace 仓库，github 网络抖时 fetch 无限卡死 → 单线程 node 被拖住 → UI 卡、点停止超时（`signal timed out`）

## 5. Git 脱敏提交技能（任意项目通用）

- 命令：`cd <项目> && git-sanitize-push "说明"`（软链在 `~/.local/bin`）
- 脚本：`scripts/tf-git-push.sh`（主流程）+ `scripts/sanitize.py`（脱敏逻辑）
- 敏感词映射：`~/.config/git-sanitize.conf`（本地私有，勿提交；新增敏感词往里加「源词=目标词」）
- 双轨模式：本地 main/master 留真实版（不 push），远程只收脱敏快照
- 流程：本地真实版提交 → 基于远程建快照分支 → 脱敏 → push（8 次重试）→ 清理

## 6. 常见故障速查

| 现象 | 根因 | 处理 |
|---|---|---|
| push 报 `Empty reply from server` | github.com:443 被墙（https） | remote 改 ssh |
| push 报 `denied to deploy key` | ssh 是 deploy key 非账号级 | 升级账号级 key |
| dsh web 卡、点停止超时 | git fetch 卡死拖住单线程 node | 见第 4 条；杀 `git fetch --prune` |
| dsh web 崩溃 ABRT | V8 堆 OOM | 见第 1 条（已调 8GB） |
