# taskflow（tf）—— 多材料 · 多步骤 · 多超算 VASP/MACE 流水线管理器

> 一句话：用一条 `tf` 命令，在多个超算上自动跑完成百上千个材料的 VASP / MACE 多步计算，全程只读巡检、异常诊断、自动续跑。

> **v2.0 重构**：本目录是重构版——原 7463 行单体脚本已拆成 `tfpkg/` 包（`bin/tf` 为入口），代码导航见 [`CONTEXT.md`](CONTEXT.md)。原始单体保留在 `versions/v1.0/tf` 作对比基准。

## 能干什么

- **多材料**：一个项目根下几千个结构（如 `C20/qHPC20`、`Ag/qHPC20_Ag1C20_s0`）统一管理，逐材料多步流水线（弛豫 → 静态 → 后处理）自动推进。
- **多技能**：16 个技能（`-tt`）覆盖 VASP 能带/弹性/电热导/晶格热导/结构优化，以及 MACE 同类 + 声子 + MLFF 训练 + 替代模型。
- **多超算**：jzzn（CPU 真 SLURM）、a800（A800 GPU 真 SLURM）、3090（无 SLURM 的 fakeslurm 垫片服务器），换超算只改一个 `hpc` 名。
- **全自动**：`auto_advance` + `tf monitor` 后台监控，作业算完自动拉结果、自动提交下一步；挂死作业自动 `scancel`+续跑（`hang_check`）。
- **省心巡检**：`tf summary --diff` 无变化输出 0 字节，有变化才吐几行——适合 AI / cron 定时巡检。

## 快速开始

```bash
# 1. 安装（把 tf 放进 PATH）
git clone <repo> ~/software/taskflow
ln -s ~/software/taskflow-v2.0/bin/tf ~/.local/bin/tf   # 确保 ~/.local/bin 在 PATH（.bashrc/.profile 里）
tf --version

# 2. 配置（全局 tf.yaml：ssh 别名、项目根、技能 work_dir、auto_advance）
vi ~/software/taskflow/setting/tf.yaml

# 3. 初始化一个材料项目（生成 project_setting）
cd 你的材料根目录 && tf -tt opt-mace-cpu init

# 4. 开跑 + 后台监控
tf auto on                      # 开全局自动推进
tf -tt opt-mace-cpu monitor -d    # 后台监控：自动拉结果 + 自动提交下一步

# 5. 巡检
tf summary --diff               # 首选巡检：无变化 0 字节
tf -tt opt-mace-cpu summary     # 只看某技能
```

## 三个超算

| 集群 | ssh 别名 | 类型 | 适用技能 |
|---|---|---|---|
| jzzn | `jzzn` | CPU，真 SLURM（cpu192） | VASP 各技能 + MACE CPU 类 |
| a800 | `A800` | A800 GPU，真 SLURM | GPU 类技能 |
| 3090 | `wangchao_3090` | 8×RTX3090，无 SLURM（fakeslurm 垫片） | MACE GPU 类（注意：垫片无排队，需靠 max_jobs 节流） |

## 11 个技能

VASP：`band-dft-cpu` 能带 / `elastic-dft-cpu` 弹性常数 / `ke-dft-cpu` 电子热导率 / `kl-dft-cpu` 晶格热导率 / `opt-dft-cpu` 结构优化+能量
MACE：`kl-mace-cpu`/`kl-mace-gpu` 晶格热导率 / `opt-mace-cpu`/`opt-mace-gpu` 结构优化+形成能 / `phonon-mace-cpu` 声子谱 / `mlff-mace` MLFF 训练

## 大规模实战（2926 材料 · opt-mace-cpu · jzzn）

一次跑完 2926 个金属掺杂富勒烯结构（38 金属 × 各笼型 × 各位点）的 MACE 结构优化 + 形成能，经验记录如下，供新项目参考：

- **本地模式（v3）**：输入文件以本地项目目录为准，超算只是算力；每个项目自己的 `project_setting/`。
- **每技能并发上限 `max_jobs`**：大体系批量提交靠它节流（`task_types.<技能>.max_jobs`），到上限后剩余任务先本地生成输入待命，自动补交——是正常状态，不是故障。
- **共享 project_setting（体系级布局）**：几千材料时用一份 `project_setting`（`local_root` 指向项目根），避免每材料一份配置把扫描拖慢；对应地 `tf` 已修复共享布局下 `-p <材料>` 定位。
- **大体系采集自动分块**：`tf` 会按 `TF_COLLECT_CHUNK`（默认 500）把同组材料分块 ssh 采集，避免 "Argument list too long"。
- **2D 结构真空轴规范**：工作流要求 2D 结构真空沿 c 轴（第 3 个晶格矢量），生成结构时注意；已算过的若真空在别的轴，需旋转晶格（保持笛卡尔坐标）再重跑。
- **形成能参考化学势 μ**：`scripts/mace_mu/` 提供本地算 38 金属单质每原子能量的固化脚本（`setup_local.sh` + `run_mu.sh`），产出 `MU = ...` 单行填进 step3 配置。

## 关键修复记录（本仓库已含）

1. 共享（体系级）布局下 `-p <材料>` 定位失效 —— 已修复（预过滤保留共享段）。
2. `max_jobs` 并发计数把"已完成作业残留状态（OTHER）"误计入 —— 已修复（`_BUSY_KINDS` 只数 R/PD）。
3. 大体系采集 ssh 命令超长（Argument list too long）—— 已修复（`TF_COLLECT_CHUNK` 默认 500 自动分块）。
4. 3090 fakeslurm 垫片无排队会瞬间拉起几千作业压垮服务器 —— 该平台务必配 `max_jobs` 节流，或改用真 SLURM 集群（jzzn/a800）。

## 提交代码到 GitHub

改完代码，一键「脱敏 → 提交 → 推送」到远程（本地真实版 + 远程脱敏版双轨，真实用户名/超算路径不会泄露）：

```bash
bash scripts/tf-git-push.sh "你的提交说明"
```

- 敏感词映射在 `setting/git-sanitize.conf`（本地私有、不进仓库），新增敏感词往里加一行「源词=目标词」
- github 走 `ssh.github.com:443`（绕过 22 端口封锁），带 8 次重试防卡

## 文档

- 完整手册：`TASKFLOW.md`
- AI/监控接入规范：`AGENTS.md`
- 各技能细节：`skill/<技能>/README.md`
