# taskflow-v1.0 —— 隔离开发副本（ISOLATED DEV FORK）

> 本目录 **不是** 生产仓库。生产仓库是 `~/software/taskflow-v2.0`（用户明确要求：
> "不要修改目前这个版本"）。这里是从 v2.0 复制出来的隔离副本，**所有新特性开发都在
> 本副本内进行**，v2.0 一行不改。

## 0. 来源与基线

| 项 | 值 |
|---|---|
| 复制来源 | `/home/wangchao/software/taskflow-v2.0` |
| 复制时间 | 2026-09-14 |
| 上游提交 | `56fc99f`（feat(fc-fit): add the fc-fit skill） |
| 复制方式 | `rsync -a`（保留当时工作区 91 个未提交改动，原样带过来） |
| 基线快照提交 | `baseline: 隔离快照 …`（分支 `dev-v1.0`） |
| 排除内容 | `tmp/`（672M 临时区）、`__pycache__`、`setting/.tf_state_cache.json`、`setting/.tf_watch.log`、`setting/.tf_hung.json`、`setting/.tf_summary_*.txt`、`setting/.tf_watch.pid` |

git 远端已重命名为 **`origin-v2.0`**（避免误 push 到 GitHub 上的 v2.0 仓库）。
要 push 到新仓库时先 `git remote rename origin-v2.0 origin && git remote set-url origin <新地址>`。

## 1. ★ 安全约定（重要）

副本的 `setting/tf.yaml` 与 v2.0 **共用同一批 project_roots**（同一批真实项目、
同一批超算账号）。也就是说：**在副本里敲错命令，会真的往集群提交/取消作业。**

因此副本已被改成：

- `auto_advance: false` —— 副本内任何 tf 命令都**不会**自动提交作业；
- `auto_watch: false` —— 副本绝不拉起后台监控（避免与 v2.0 的 monitor 抢提交）。

开发本副本时只允许跑**只读命令**：
`tf skills` / `tf schema` / `tf history` / `tf list` / `tf summary` / `tf -V` / `tf config`。
**禁止**在副本里跑 `tf start/stop/retry/rerun/clean`（会动真集群）。

## 2. 怎么跑副本

```bash
cd ~/software/taskflow-v1.0
python3 bin/tf skills          # 只读：列出技能（走副本自己的 skill/ 与 setting/）
python3 bin/tf -V
```

`~/.local/bin/tf` **仍指向旧仓库 `~/software/taskflow/versions/v1.0/tf`**，未被本副本改动。

## 3. 本副本做了什么（v1.0 加技能友好化）

| 计划 | 状态 | 内容 |
|---|---|---|
| 建议 1.3 | ✅ 已完成 | `io_schema` 段（技能自报吃什么/吐什么/有哪些旋钮）+ 校验 |
| 建议 1.2 | ✅ 已完成 | `flow` 段（整条流程 + 产物能喂给谁） |
| 建议 1.1 | ✅ 已完成 | `_corrections/` handler 库（base + 4 handler + `tf correct`） |
| 建议 3.1 | ✅ 已完成 | 零配置自动发现已在，本次补齐自描述的自动校验/展示 |
| W1–4 | ✅ 已完成 | `tf schema [技能]`（`--json` / `--strict`） |
| W5–8 | ✅ 已完成 | `history.jsonl`（采集时自动记录）+ `tf history` |
| 模板 | ✅ 已完成 | `skill/_template/`（复制即上线，实测零配置被发现） |
| W9–12 | ⏳ 设计稿 | `result/_cache/` 结果缓存（设计草案见 ROADMAP 第 3.1 节，未实现） |

详见 `V1.0-ROADMAP.md`。

## 4. 离线自测（不碰集群，随时可跑）

```bash
cd ~/software/taskflow-v1.0
python3 tmp/test_v1_skillspec.py     # 30 项：自描述校验 + 纠错库加载/匹配/隔离
python3 tmp/test_v1_history.py       # 17 项：history 记录/过滤/--since
python3 tmp/test_v1_correct_cli.py   # 13 项：tf correct / tf diagnose 接入（假数据）
python3 tmp/tf_smoke.yaml            # 最小配置：只指向仓库自带本地沙盒 test/tf_test
python3 bin/tf -c tmp/tf_smoke.yaml list      # 主路径冒烟（本地，不 ssh）
python3 bin/tf -c tmp/tf_smoke.yaml history   # 看历史
```

三个测试脚本都是纯本地断言（不连超算、不写项目、不提交作业），失败返回非零。
