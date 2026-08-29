# taskflow (tf) 使用说明

> 本文是 taskflow 的**总文档**：用户手册 + 技能总览 + 技能开发规范。
> 配套文件：`AGENTS.md`（给 LLM/agent 的操作规范）。原独立文档 SKILL_DEV.md（技能开发规范）、
> skill_prompt_mlff-mace.md（mlff-mace 设计提示词）、使用说明.md（step.conf 开发记录）的内容均已并入本文
> （分别见第 7 章、§6.9、§5.3），不再单独保留。

**当前版本 1.0**（版本号自 1.0 起重新计数，与 `tf -V` 一致；文中不再逐条标注历史版本号）。

VASP/DFT 与 MACE/MLFF 多材料·多步骤·**多任务类型**流水线管理框架（SLURM + 无调度器垫片）。单文件 Python，**零第三方依赖**（内置迷你 YAML 解析器，装了 PyYAML 会优先用），超算端零安装、零状态文件。

## 文档地图

| 章节 | 内容 | 读给谁 |
|---|---|---|
| 0 | 适用范围与能力边界 | 所有人 |
| 1–5 | 安装、核心概念、命令、状态、配置 | 所有用户 |
| 6 | 11 个技能逐一说明（含 mlff-mace） | 用户 / agent |
| 7 | 技能开发规范（原 SKILL_DEV.md 内容） | 技能作者 / AI |
| 8–11 | 工作原理、agent 接入、目录结构、文档清单 | 所有人 |

## 0. 适用范围与能力边界

> 一句话：tf 是**调度器 + 判据检查器**，不是计算引擎。物理量是 VASP / MACE / phono3py / AMSET / BoltzTraP2 算的，tf 只负责把它们串成流水线、盯收敛、接下一步、判成败。环境依赖见根目录 `environment.yml`。

**覆盖范围（能做什么）**

- 11 个技能、两类引擎：**VASP/DFT**（能带、弹性常数、电子热导率、晶格热导率、结构优化+能量）＋ **MACE/MLFF**（晶格热导率、结构优化+形成能、声子谱、随机位移法 MLFF 训练）。
- 多材料 × 多步骤 × 多任务类型编排：`needs` 显式 DAG 依赖、扇出步骤（fanout，一步下 N 个并行作业）、可选步骤组、断点续跑、跨技能共用结果（如 kl-mace 接 kl-dft-cpu 的 BORN）。
- 三种运行环境：真 SLURM（jzzn cpu192 / a800 GPU 分区）、无 SLURM 服务器（3090，fakeslurm 垫片），一台项目用 `tf hpc` 切换。
- 自动化：auto_fetch / auto_advance / auto_watch（零输入全自动）、retry / rerun / stop / clean、内置判据自动判成败、0D/2D/3D 维度自动判定。
- 技能开发：`skill/<名>/skill.yaml` 自描述，放进 `skill/` 即被自动发现，不改 tf 主程序（见第 7 章）。

**不在范围内（不能做什么）**

- **不提供计算引擎**：不内置 VASP/MACE/phono3py/AMSET 等，全部依赖外部程序；tf 本身是单文件 Python、零第三方依赖（PyYAML 可选）。
- **需要外部许可 / 资产**：VASP 需商业许可（DFT 类技能）；MACE 类技能需要一个**预先训练好的 `.model` 势文件**（`MACE_MODEL`，tf 不负责训练它——唯一例外是 mlff-mace 本身产出势）。
- **物理覆盖面有限**：目前只有上述 11 类。**没有**光学性质、磁性专项、分子动力学（MD）、NEB 过渡态、自旋轨道耦合（SOC）等技能。
- **MACE 的原理性缺失**：MACE 势给不出 Born 有效电荷和 ε∞（势里无电荷响应）——极性材料算晶格热导率需从 kl-dft-cpu 的 DFPT 接 `NAC_BORN`；`phonon-mace-cpu` 只算 2 阶力常数（fc2），不产 fc3 / κ。
- **0D 分子支持有限**：只有 band-dft-cpu step1 开了 `MOL_BRANCH`；弹性 / 热导 / 形变势对分子无定义，未开 0D 的技能会明确报错。
- **只算形成能、不算凸包**：opt-dft / opt-mace 产出 `E_form`（凸包上方能量 E_hull 的原料），凸包比对需用 pymatgen / MP API 离线另做。
- **mlff-mace 只产出势**：只做「随机位移法训练 + 验收」产出一个验证过的 `.model`，不做 κ 生产计算、fc3 生产拟合、生产 MD（fc3/κ 可信度由下游 kl-mace-* 背书）。
- **单用户本地驱动**：调度入口在本地（WSL），集群侧只是算力；不是集群资源管理器（依赖 SLURM 或 fakeslurm 垫片）。Windows 原生不支持（需 WSL）。
- **网络假设**：mlff-mace 要求集群有 `REPLAY_XYZ` 基座（集群无外网时拿不到就报错退出，不降级）。

**成熟度与未验证项**

| 版本 | 技能 |
|---|---|
| 1.3 / 1.2 | band-dft-cpu / elastic-dft-cpu（最成熟） |
| 0.2 | kl-dft-cpu |
| 0.1 | ke-dft-cpu、opt-dft-cpu、kl-mace-cpu/gpu、opt-mace-cpu/gpu、phonon-mace-cpu、mlff-mace |

- mlff-mace 的 GPU 路径（a800/3090 `submit_mace.tpl` + CONDA_ENV）**已预留、未实测**（jzzn 登录节点无 GPU 分区，DEVICE=auto 恒落 CPU）。
- 各技能 / 各超算的实时状态以 `tf summary` 与 `skill/<名>/README.md` 为准。

---

## 1. 安装

推荐版本化布局（详见第 10 节"目录结构与版本管理"）：

```bash
mkdir -p ~/software/taskflow/versions/v1.0 ~/.local/bin
cp tf ~/software/taskflow/versions/v1.0/tf && chmod +x ~/software/taskflow/versions/v1.0/tf
cp tf.example.yaml ~/software/taskflow/setting/tf.yaml
ln -sf ~/software/taskflow/versions/v1.0/tf ~/.local/bin/tf
tf --version
```

---

## 2. 核心概念

- **任务类型（tt）**：一类计算 = 一套步骤流水线 = 一个技能。流水线由 `skill/<技能名>/skill.yaml` **自描述**（tf 自动发现，全局 tf.yaml 不用再抄 steps）。
  当前 11 个技能：

  | 类型 key | 中文名 | 引擎 | 版本 |
  |---|---|---|---|
  | `band-dft-cpu` | 能带 | VASP/DFT | 1.3 |
  | `elastic-dft-cpu` | 弹性常数 | VASP/DFT | 1.2 |
  | `ke-dft-cpu` | 电子热导率（AMSET） | VASP/DFT | 0.1 |
  | `kl-dft-cpu` | 晶格热导率（第三阶力常数+BTE） | VASP/DFT | 0.2 |
  | `opt-dft-cpu` | 结构优化 + 能量/形成能 | VASP/DFT | 0.1 |
  | `kl-mace-cpu` / `kl-mace-gpu` | 晶格热导率 | MACE | 0.1 |
  | `opt-mace-cpu` / `opt-mace-gpu` | 结构优化 + 形成能 | MACE | 0.1 |
  | `phonon-mace-cpu` | 声子谱（仅 2 阶） | MACE | 0.1 |
  | `mlff-mace` | 随机位移法 MLFF 训练（产出 MACE 势） | VASP(标注)+MACE | 0.1 |

- **project（-p）**：材料项目，如 `C20/qHPC20`。
- **job（-j）**：项目里的一个步骤，可写步骤全名 / label / 序号，**必须配 -p**（不带 -p 时 `-j` = 对全部材料只操作该步骤）。序号是 `skill.yaml` 里的 `seq`（画图步用小数，如 3.1；带隙子步用 2.1~2.35）。
- **命名规则**：同一类型下项目名不允许重复（启动即报错）；不同类型下允许同名。`-p` 不带 `-tt` 时跨类型解析，唯一即用，重名会提示补 `-tt`。
- **本地模式（v3）**：输入文件（POSCAR 等）以本地项目目录为准，超算只做计算服务，目录树 = `work_dir + 项目相对路径`。类型配置写 `local_root` 即启用；只写 `root` 则是 v2 远端模式，两者可混用。
- **多超算**：`setting/<hpc>.yaml` + `setting/<hpc>/templates/` 定义一台超算；状态表 `hpc` 列显示每项目实际用的机器，`tf hpc` 切换（见 5.4）。
- **公共池 `skill/_common/`**：多个技能共用的引擎与模板（`relax_common.py`、`dim_common.py`、`stepconf.py`、`_common/mace/` 等），技能目录里没有的文件回落到池子里取（见 7.2）。

---

## 3. 命令参考

```
tf summary                        巡检首选：只读极简汇总（每任务类型一行 done/run/err/
                                  scancel/wait 计数 + FAIL 清单），省 token，绝不提交。
                                  尊重 -tt/-status/-x/--hide-done 过滤，如
                                  tf -status error summary 只看失败的
tf summary --diff                 巡检更省：与上次快照对比，无变化输出 0 字节（静默），
                                  有变化才输出汇总。快照按过滤范围分开存在配置文件
                                  目录（.tf_summary_*.txt），首次运行建立基线
                                  （summary 同 list 走本地缓存，--refresh 强制刷新）
tf list                           只读状态总表：不 auto-fetch、不 auto-advance，纯查看、
                                  绝不提交（巡检/查看优先用它，别用 status）。
                                  默认 TF_CACHE_TTL 秒内（默认 60）复用本地采集缓存、
                                  跳过 ssh 秒开；--refresh 强制重新采集，TF_CACHE_TTL=0 关闭
tf [ROOT] / tf status             状态总表 + auto-fetch + auto-advance（会拉文件、会提交）。
                                  注意：裸 tf（无参）只报版本，不采集
tf -tt band-dft-cpu                       只看某类型（也支持 tf -tt band-dft-cpu summary 只汇总该类型）
tf -tt band-dft-cpu -p MAT status         单材料详情
tf [-tt TT] [-p MAT] start       开始：输入没生成先 gen 再提交；无 -p = 推进全部
tf [-tt TT] [-p MAT] stop        取消作业。取消的步骤打 scancel 标记
                                  （本地材料目录 .tf_scancel.json）：状态列显示
                                  scancel，auto_advance 和批量 start 都不会再动它。
                                  显式重跑：-p MAT start（保留文件直接重交）/retry/rerun，
                                  或跨材料 -status scancel start（retry/rerun）；
                                  重交成功/rerun/clean 后标记自动清除，
                                  步骤出现新作业或已完成时标记也会自愈
tf [-tt TT] [-p MAT] retry       用现有输入文件重交（在超算手改 INCAR/KPOINTS 后用它，
                                  tf 不动超算上的文件，直接 sbatch 提交）
tf -p A B retry                  同时操作多个项目（也支持 -p A,B 逗号分隔；
                                  start/stop/rerun/clean/status/dir/fetch 同样适用）
tf [-tt TT] [-p MAT] rerun       删除旧的生成文件 → gen 重新生成 → 提交
tf -j STEP rerun                 跨材料只重做该步骤（gen 脚本改动后一键修复全部材料；
                                  自动跳过 done 的和前序未完成的，加 -f 可强制）
tf -j STEP start/stop/retry/clean  同理：-j 不带 -p = 对全部材料只操作该步骤
tf -x A,B ...                    任何命令加 -x 跳过指定项目（逗号分隔，全名或 basename）
tf -status ST ...                只保留含指定状态步骤的材料，对任意命令生效：
                                  tf -status scancel          只看被 stop 取消的
                                  tf -status scancel start    把它们全部重跑（保留文件重交）
                                  tf -status error retry      重交全部失败步骤
                                  状态词 done/running/pd/error/waiting/scancel，逗号分隔
tf [-p MAT] [-j STEP] clean       只删不建回到 PREP：无 -p=全部材料（本地+超算只留 POSCAR）；
                                  -p C20=该体系全部材料；-p -j=单个步骤目录。
                                  多技能：-tt elastic-dft-cpu clean 只清 elastic-dft-cpu 的产物并从项目配置
                                  移除 elastic-dft-cpu 段（band-dft-cpu 段和配置目录保留；
                                  本技能是最后一个段才整目录删 project_setting）
tf skills                        列出已发现的所有技能（版本、步骤数、清单路径、警告）
tf -tt TT dir                    输出类型根目录路径
tf -p MAT [-j STEP] dir          输出材料/步骤在超算上的目录路径（只输出路径，便于拼接命令）
tf [-p MAT] fetch                手动强制拉回结果（status 时已自动保存完成的步骤到 result/，
                                  项目 setting.yaml 里 auto_fetch: false 可关闭）。
                                  按 result/<step>/.tf_fetched 戳记判"已抓取"，
                                  不再每次重拉；步骤重提交后自动清戳重拉，
                                  tf fetch 手动拉不受戳记限制（结果不完整时就用它强制重拉）
                                  tf fetch --all 把每个步骤整个目录拉回
tf -p A,B hpc 集群名              指定项目跑哪台超算：材料级写 project_setting/hpc.yaml；
tf -tt TT -p A,B hpc 集群名        技能级写 材料/<技能>/hpc.yaml（只改该技能，优先级最高）。
                                  必须搭配 -p，只动指定项目、老项目不变；只影响之后提交的作业
tf [-tt TT] -p MAT -j STEP conf    查看/修改该步骤的 step.conf（分层合并后的最终值）。
                                  不带 --set = 打印合并结果 + 各层来源；
                                  --set 节.键=值 写进本材料 project_setting/templates/<步骤>/step.conf
                                  （键名不带点 = 写入 [params] 节；值留空 = 删键）。
                                  例：tf -tt kl-mace-cpu -p X -j 2 conf
                                      tf -tt kl-mace-cpu -p X -j 2 conf --set params.METHOD=random
                                      tf -tt opt-dft-cpu -p X -j 3 conf --set MU="C:-9.0 Li:-1.9"
tf auto [on|off]                 一键开关全局 auto_advance（动目录/恢复备份前先 off）
tf init                          批量初始化：当前目录下所有项目生成 project_setting/
tf -p MAT init                   只初始化该项目（如 -p C20/qHPC20 → C20/project_setting）
tf -p MAT -j STEP init           只生成该步骤输入文件（gen），不提交——提交前可先检查
tf watch [-i 秒]                 监控模式（前台）：每 interval 秒（默认 300）自动
                                  重新采集 → auto-fetch → auto-advance；
                                  状态有变化才打印总表，否则一行心跳；Ctrl+C 退出。
                                  每轮自动检测配置文件改动并重载（tf.yaml、project_setting/*.yaml、
                                  材料/技能的 hpc.yaml）——改配置或换 tf 版本后不用重启监控
tf watch -d                      后台监控（推荐）：不占终端，日志/pid 固定在
                                  tf.yaml 所在目录（.tf_watch.log，tail -f 查看）；
                                  tf watch --stop 任意目录可停止
tf watch --install / --uninstall crontab 保活：每 10 分钟检查，监控死了自动
                                  拉起（重启/WSL 关闭后自动恢复），不会重复启动
tf -tt TT migrate-subdir [--dry-run | -y]  迁移材料到 技能子目录 布局（band-dft-cpu 已迁移完，
                                  新技能默认 skill_subdir: true，一般用不到）
tf -tt TT adopt [--dry-run | -y]  接管"人手工搬进 材料/<技能>/"的目录（先 tf auto off）
tf json / tf config              JSON 输出（全量，token 大）/ 打印示例配置
```

**零输入全自动（推荐配置）**：tf.yaml 里写 `auto_advance: true` + `auto_watch: true`，再执行一次 `tf watch --install`——之后**不需要敲任何命令、不需要手动挂监控**：监控死了任何 tf 命令顺带拉起（auto_watch），重启/WSL 关闭后 crontab 保活拉起（--install）。想彻底关掉后台监控：`auto_watch: false` + `tf watch --stop` + `tf watch --uninstall`。WSL 注意：保活依赖 WSL 里的 cron 服务在跑（`sudo service cron start`；wsl.conf 开 systemd 则自动）。Windows 侧更稳的替代：任务计划程序加"登录时运行" `wsl -e bash -lc "tf watch -d"`。

状态总表每个项目**两行**：第一行是各步骤状态词，第二行是 job 实况（已去掉总体 Status 列）。

- 状态词：`done` 完成 / `running` 运行中 / `pd` 排队 / `error` 未通过判据 / `waiting` 未开始（输入未生成、就绪待交、被前序阻塞都算）/ `scancel` 被 tf stop 取消（打标记，auto 不会重跑，显式重跑后自动清除）
- 第二行：running → `节点 任务号 已跑时长`（如 `cu41 3569183 0:42:11`）；pd → `任务号 (原因)`；scancel → `已取消(原任务号)`；其余 `-`
- `hpc` 列 = 该项目使用的超算；`dim` 列 = 0D/2D/3D 判定

选项：`-tt` 类型、`-p` 材料（完整名 `C20/qHPC20` 或唯一 basename `qHPC20`；多个用逗号分隔或空格跟在后面）、`-j`/`-job` 步骤（全名/label/序号）、`-x` 跳过指定项目、`-status` 按步骤状态过滤材料、`-c` 配置、`--host`、`-u` squeue 用户、`-f` 强制（先取消再交）、`-y` 免确认、`--refresh` list/summary 强制跳过本地缓存重新采集。帮助：`tf -h` 或 `tf help`（含常用示例）。

SLURM 作业名：提交时统一改为 `材料-任务类型-步骤label`（如 `qHPC20-band-S1_opt`），覆盖 submit.sh 里原有的 `--job-name`/`-J`，squeue 里一眼对应项目。**3090 已装真 SLURM 24.05.8**（分区 cpu192 默认 + gpu 6 卡），sbatch/squeue/scancel 走 `/usr/bin`。

新增材料：把带 POSCAR 的目录放进项目根（如 `C20/qHPC20new/`），`tf` 状态表末尾会提示"发现新材料目录未初始化"；`tf init` 是增量的——只给新材料生成 `project_setting/`，已初始化的自动跳过。init 后 `tf start` 开始；配了 `auto_advance: true` 则下次 `tf` 自动开算。

---

## 4. 状态语义

| 状态词 | 含义 | 触发条件 |
|---|---|---|
| `PREP` | 输入未生成 | 步骤目录不存在 |
| `TODO` | 已生成待提交 | 目录在、没作业、判据不过 |
| `R` / `PD` | 运行中 / 排队 | squeue 里该目录有作业 |
| `OK` | 完成 | 判据返回 True |
| `FAIL` | 算完了但判据不过 | 无作业 + 判据 False，`diag` 给原因 |
| `WAIT` | 被前序步骤阻塞 | 前一步没 OK |
| `SCANCEL` | 被 tf stop 取消 | 打了 .tf_scancel.json 标记 |

- 画图/后处理等 `run: gen` 步骤不用这套词，用 `completed` / `not started` / `error`（无 running/pd）。
- 扇出（fanout）步骤显示 `3/5 2R 0PD`（完成数/总数、在跑、排队）。
- `diag` 是判据诊断（如 `force not converged`、`pressure 12.3kB > 5`、`WAVECAR too small`），显示给人/agent 看。

---

## 5. 配置

### 5.1 全局 tf.yaml + 项目配置

**配置跟着项目走**。全局 tf.yaml 只登记站点信息和每技能的工作根；**流水线步骤定义在 `skill/<技能>/skill.yaml` 里**（技能自描述），覆盖优先级：

```
skill/<技能>/skill.yaml  <  全局 tf.yaml 的 task_types.<key>  <  项目 tf_<项目名>.yaml
```

```yaml
# ~/software/taskflow/setting/tf.yaml（全局，实际现状）
host: jzzn                            # 默认 ssh 别名（项目 hpc.yaml 可覆盖）
remote_path_prefix: /home/wangchaoyue852/software/pybin    # 只注入 python/python3/vaspkit 软链（供 gen 脚本用 numpy/pymatgen/vaspkit）；sbatch 走各机器真 SLURM
project_roots:                        # 项目根列表：扫描其下 project_setting/tf_*.yaml（含一层子目录）
  - /mnt/d/tf_data
  - /home/wangchao/software/taskflow/tf_test
  - /mnt/d/tf_data/Fullerene_Network/gen_metalfullence/doped/intercalation
auto_advance: true                    # status/watch 时自动提交可开始的步骤
auto_watch: false                     # 设为 true = 任何 tf 命令顺带拉起后台监控
task_types:                           # 只写与 skill.yaml 不同的站点字段（work_dir 必须配）
  band-dft-cpu:
    work_dir: /public/home/wangchao/Fullerene_Network/work
    max_jobs: 100                     # 本技能同时提交的作业上限（只卡 sbatch）
  elastic-dft-cpu: {work_dir: /public/home/wangchao/Fullerene_Network/work, max_jobs: 100}
  ...
```

项目配置规则：

- **命名**：`tf_<项目名>.yaml`（如 `tf_C20.yaml`），**全局唯一，禁止重复**——两个项目放同名文件会直接报错并列出两个路径。
- **local_root 推荐写 `".."`**（= 体系根）：材料名带 `C20/` 前缀，超算目录 = `work_dir/C20/qHPC20`，与本地目录树一致。缺省 = project_setting 父目录。
- **字段继承**：没写的字段（steps/skill_dir/hpc/gen_need/work_dir）自动继承全局 tf.yaml 同 key 类型，再往下继承 skill.yaml；写了就覆盖。
- **分段合并**：多个项目配置定义同一个类型 key = 该类型的多个分段，各自发现材料，表格里合并显示。
- 用了项目配置，全局就不要写 local_root；新增项目 = 放好 POSCAR 跑 `tf init`，不用动全局配置。

### 5.2 project_setting/（就近优先）

从材料目录向上找最近的 `project_setting/`（如 `C20/project_setting` 对其下所有材料生效，`C20/qHPC20/project_setting` 可再覆盖单个材料）。用 `tf -p MAT init` 生成，含：

- **setting.yaml**（路径与结果，占位符 `{matdir} {mat} {root}`）：
  ```yaml
  base_dir: "{matdir}"              # 项目基准目录
  result_dir: "{matdir}/result"     # fetch 拉回位置（result/<step>/）
  log_dir: "{matdir}/log"           # tf 操作日志 tf.log
  work_dir: /public/home/...        # 可覆盖类型的超算工作根
  fetch_files: [INCAR, POSCAR, POTCAR, KPOINTS, KPOINTS_OPT, kpath.json, submit.sh, OUTCAR, CONTCAR, EIGENVAL, vasprun.xml, queue.out]
  ```
  `fetch_files` 可扩展——不同体系要留的输出不同，往里加文件名即可。
- **hpc.yaml**（超算与模板映射）：
  ```yaml
  name: jzzn              # 表格 hpc 列显示名；换超算 = 改这里（模板自动切到 setting/<name>/templates/）
  ssh_host: jzzn          # 实际 ssh 别名（换超算改这里）
  template_map: {}        # 逻辑名 → 实际模板文件（集中式模板下一般留空）
  ```
  项目没有 hpc.yaml 时回退到包内 `setting/<hpc>.yaml`（默认 jzzn）。
- **templates/**：项目级模板与 step.conf 覆盖（优先级最高的覆盖点）。

**资源查找链（gen_need 的每个文件、模板逻辑名）**：

```
① 项目内覆盖：材料/<技能>/templates/、project_setting/templates/（含 <步骤名>/ 子目录）
② 集中式 HPC：setting/<hpc_name>/templates/（含 <步骤名>/ 子目录）
③ 技能目录：skill/<技能>/templates/<步骤名>/ → templates/ → 根目录
④ 公共池：skill/_common/templates/ → skill/_common/
⑤ gen_dir（远端兜底）
```

推送/渲染到超算时文件名始终是逻辑名，gen 脚本不用改。

### 5.3 step.conf 三层合并与 tf conf

**参数一律走 step.conf**，不硬编码在 gen 脚本顶部。三层合并：

```
skill 出厂默认（skill/<技能>/templates/step.conf）
  → 项目 templates/step.conf（本材料所有步骤共用）
  → 项目 templates/<步骤名>/step.conf（只覆盖该步骤）
```

tf 推送时先在本地把各层合并成**一份**带 `# <- [N]` 来源注释的文件再推，超算上只有一份、零回落逻辑。查看/修改：

```bash
tf -tt kl-mace-cpu -p <材料> -j 2 conf                    # 看合并后的最终值 + 各层来源
tf -tt kl-mace-cpu -p <材料> -j 2 conf --set params.METHOD=random
tf -tt opt-dft-cpu  -p <材料> -j 3 conf --set MU="C:-9.0 Li:-1.9"
```

`--set` 永远写进**本步的项目文件**，绝不动 skill 出厂默认。

语法：全文只有 `KEY = VALUE`，行尾 `#` 或 `!` 之后是注释。节名即语义：

| 节 | 作用 |
|---|---|
| `[params]` | 脚本行为参数，不写进 INCAR |
| `[submit]` | 覆盖 `submit.sh` 的 Slurm 行（键：nodes/ntasks_per_node/ntasks/cpus_per_task/qos/partition/time/job_name/gres/mem；留空值 = 删除该键回模板默认） |
| `[incar]` | 覆盖从上一步继承来的 INCAR 标签 |
| `[incar.delete]` | 删除继承来的标签，每行一个光秃秃的标签名 |
| `[incar.final]` | 在脚本自动算完 `NBANDS`/`KPAR`/`MAGMOM` 之后再覆盖 |

INCAR 生效顺序固定：`上一步继承 → [incar] → 脚本自动计算 → [incar.final] → [incar.delete]`

覆盖核数的典型用法：

```bash
tf -tt opt-dft-cpu -p X -j 1 conf --set submit.ntasks_per_node=12   # VASP 类(MPI)
tf -tt kl-mace-cpu -p X -j 2 conf --set submit.cpus_per_task=12  # MACE 类(torch线程)
# 改完重新 gen：tf -p X -j N init -f 只生成不提交，检查后再 start
```

类型支持 `str` / `int` / `float` / `bool` / `words`（空白切分）/ `elemmap`（`Mn:5.0 In:0.0` → dict）。
脚本侧通过 `stepconf.load(CONF_SPEC, step_name)` 读取（技能作者视角见 7.7）；**`[params]` 里没在 CONF_SPEC 声明过的键直接报错**，防止"改了个没人读的变量还以为生效了"。

### 5.4 多超算与集中式提交模板（setting/<hpc>）

把「技能逻辑」与「超算提交」分离：技能目录只放与超算无关的脚本/模板；**所有提交模板（submit_*.tpl）集中到 `setting/<hpc>/templates/`**，逻辑名即文件名，换超算 = 改一个 name，不用动技能代码。

当前三台超算：

| name | ssh 别名 | 硬件 | 调度 | 环境 | work_dir（集群默认） |
|---|---|---|---|---|---|
| `jzzn` | `jzzn` | CPU 集群（cpu192 分区） | 真 SLURM | VASP 6.4.x；MACE 走 venv `~/venvs/mace_cpu`（torch 2.7.1+cpu） | `/public/home/wangchao/Fullerene_Network/work` |
| `a800` | `A800` | 4 节点 × 8×A800-SXM4-80GB，每节点 128 CPU | 真 SLURM（分区 a800，GRES gpu:a800） | VASP 6.4.3 GPU 版；conda `mace`（mace 0.3.16+torch cu128+phono3py+symfc+pheasy 单环境） | `/fs0/home/wangcch/work`（/fs0 已 97% 满，注意） |
| `3090` | `wangchao_3090` | 8×RTX3090 | 真 SLURM 24.05.8（分区 cpu192 默认 + gpu 6 卡） | conda `mace-gpu`；VASP 6.6.0 GPU 版（OpenACC，`~/software/vasp.6.6.0/bin/{vasp_std,gam,ncl}`，NVIDIA HPC-SDK 24.11 环境） | `/home/wangchaoyue852/taskflow/work` |

- **切换**：`tf -p qHPC20,qHPC24 hpc a800`（材料级）/ `tf -tt elastic-dft-cpu -p qHPC20 hpc a800`（技能级）。只动 `-p` 指定的项目；只影响之后提交的作业。
- **接入一台新超算（两步）**：① 照 `setting/jzzn.yaml` 建 `setting/<名>.yaml`（name/ssh_host/模板映射/集群默认 work_dir 与 conda 环境）；② 把提交模板放到 `setting/<名>/templates/`（逻辑名即文件名，如 `submit_std_2d.tpl`、`submit_mace.tpl`、`<步骤名>/submit_std_2d.tpl` 变体）。`#SBATCH --job-name` 必须写成 `{{JOBNAME}}` 占位符。
- 旧版散在 skill 里的 `submit_jzzn_*.tpl` 已移到 `setting/.migrated/submit_backup/` 备份；现有项目各自的 `project_setting/templates/` 副本不受影响（优先级最高）。

#### 5.4.1 每步可指定超算（v1.12）

默认整材料跑一台超算（`tf hpc` 切）；v1.12 起**单个步骤也能指定超算**：在 skill.yaml（或项目 `tf_<项目>.yaml`）的步骤定义里加 `hpc: <集群名>` 字段即可，例如：

```yaml
  - {seq: 2, name: step2_disp_force, label: S2_force, hpc: jzzn, ...}
```

效果：
- 该步骤的**采集/状态检查**走 `<集群名>` 的 ssh_host + work_dir（远端目录 = `<集群>.work_dir/材料/<subdir>/stepN`）；
- 该步骤的**提交模板**从 `setting/<集群名>/templates/` 取；
- 该步骤的 **gen/提交** 用 `<集群名>` 的 ssh_host；
- 材料其余步骤留在材料默认集群；已算完的步骤结果不丢（各步骤在各自集群查状态）。

典型用法：重活（VASP 大体系）放 GPU 集群、轻活/后处理放 CPU 集群，一个材料的不同步骤各跑各的。

#### 5.4.2 集群环境注入（不硬编码个人路径，v1.12）

每人机器不同，脚本里**不要写死路径**。集群相关的环境路径统一写在 `setting/<集群名>.yaml`：

```yaml
conda_sh:  /home/<用户>/miniconda3/etc/profile.d/conda.sh   # conda.sh 全路径
conda_env: mace-gpu                                        # 该集群默认 conda 环境
amset_env: amset                                           # amset 专用环境（amset deform/read/run）
```

tf 在合成每步 step.conf 时，把这些键作为 `[params] CONDA_SH / CONDA_ENV / AMSET_ENV / MACE_MODEL_DIR` **注入所有技能的步骤**（`stepconf.py` 已把它们列为通用保留参数）。gen 脚本 / 提交模板需要时从 step.conf 读，例如 amset 步骤：

```python
# gen 脚本里：优先读 step.conf 注入的集群参数，缺省才回退主机探测
_AMSET_ENV_SRC = "source %s && conda activate %s" % (CONDA_SH, AMSET_ENV)
```

换人/换机器 = 改 `setting/<集群名>.yaml`，脚本与技能代码零改动。

### 5.5 自动化开关

- `auto_advance: true`（全局 tf.yaml 顶层）：status/watch 时自动提交可开始的步骤（gen+提交一条龙，流水线算完自动接下一步；error 不自动重试，留给人/agent 判断；项目 setting.yaml 里 `auto_advance: false` 可单独关闭；可用 `tf auto on/off` 一键切换）。
- `auto_watch: true` + `tf watch --install`：零输入全自动（见第 3 节）。
- 动目录、恢复备份、大批量调整前先 `tf auto off`，完事 `on`。

### 5.6 其它配置项

- **run_steps 步骤子集**（全局或项目级均可）：`run_steps: [1]` 只跑 seq=1 的步骤；元素匹配 `seq`、`name` 或 `label`，不写 = 全部步骤。
- **max_jobs 按技能限制并发提交**：每个 `task_types.<key>` 写 `max_jobs: N`；也可写全局默认；环境变量 `TF_MAX_JOBS` 兜底。只卡「提交超算（sbatch）」：达到上限后尚未提交的任务先本地生成输入（`PREP` → `TODO`）待命，watch 每轮（默认 300 秒）发现有作业算完就自动补交。kl 类扇出步骤按实际子作业数计。
- **挂死作业自动恢复（v1.11，`hang_check`）**：watch 每轮检查 RUNNING 作业，**用进度指纹判定挂死**——记录每个作业的（`OUTCAR` 字节数, `OSZICAR` 行数），指纹连续 `hang_min_stale_rounds`（默认 2）轮不变 **且** 输出年龄 ≥ `hang_stale_secs`（默认 1.5h）才算挂死（指纹在涨 = 活着，放它继续算；OSZICAR 尾的 SCF 迭代 rms 还在降 = 慢但活着，也不判）。判定后**先诊断原因再处理**：
  - `scf`（OSZICAR 尾是 SCF 迭代行且 rms 不再降）：SCF 空转/尾部卡死——只重跑会再次挂死，`hang_fix_scf` 时自动升级 INCAR（补 `AMIX=0.1`/`BMIX=0.0001` → 改 `ALGO=All` → `NELM≥200`，**原子写 + 备份 `INCAR.bak.*`**），再从 CONTCAR 续跑重交，并给 `hang_grace_rounds` 轮宽限期（AMIX 降低使 SCF 变慢，防被再次误判）。
  - `node`（queue.err 或 `sacct` 查到的 NODE_FAIL）：节点故障，直接重跑（换节点即可）。
  - `disk`（No space）：磁盘满，**只告警不重跑**（满盘上写文件会写坏 INCAR/POSCAR）。
  - `unknown`：直接重跑。
  恢复动作：`scancel` → 轮询等作业退出 → **校验 CONTCAR 完整**（≥8 行、原子数与 POSCAR 一致，不完整则保留原 POSCAR 并告警）→ 备份旧输出 `*.hung` → 重新 `sbatch`。恢复次数受 `hang_max_retries`（默认 2）限制，计数存 `<配置目录>/.tf_hung.json`；超限只告警。**`hang_dry_run: true` 时只打印判定不动手（观察期安全模式）**。参数优先级：项目 `setting.yaml` > 技能 `task_types.<key>.*` > 全局 `tf.yaml` > 默认。关掉写 `hang_check: false`，不想自动改 INCAR 写 `hang_fix_scf: false`。
- **hide_done**：`tf --hide-done` 只显示未完成项目；全局写 `hide_done: true` 设为默认，`--show-done` 临时恢复。
- **维度自动判定（0D/2D/3D）**：`dim_common.detect_dimension()` 按 POSCAR 真空层（阈值 8 Å）判定——0 个真空方向 = 3D，1 个 = 2D（真空轴记入 workflow_method.txt 的 `DIM=`，后续步骤继承），**≥2 个 = 0D（孤立分子）**。0D 只有开了 `MOL_BRANCH` 的技能（band-dft-cpu step1）支持；弹性/热导/形变势对分子无定义，未开 0D 的技能会明确报错而不是默默跑出数。

---

## 6. 技能总览与各技能说明

各技能细节以 `skill/<技能>/README.md` 为准，本节给总览与操作要点。

### 6.1 band-dft-cpu 能带（v1.3）

| seq | 步骤 | label | 内容 | 判据 |
|---|---|---|---|---|
| 1 | `step1_PBE_opt` | S1_opt | 结构弛豫（**作业内三段**：INCAR.s1/s2/s3 + run_relax.sh，`STAGE_MODE=in_job` 默认） | `relax_injob` 收敛 |
| 2 | `step2_PBE_static` | S2_static | PBE 静态自洽（读 S1 CONTCAR 接力） | `outcar` |
| 3 | `step3_PBE_WAVECAR` | S3_WAVECAR | WAVECAR 重算 | `wavecar` |
| 3.1 | `step3_band_plot` | S3.1_plot | PBE 能带画图（登录节点，不交 SLURM） | band_summary.json |
| 3.2 | `step3_vacuum` | S3.2_vac | ★ PBEsol 真空能级对齐（登录节点） | vacuum_align_summary.json |
| 4 | `step4_HSE_band` | S4_HSE | HSE 能带（`bandgap_hse` 组，BANDGAP=hse 时） | `outcar` |
| 4.1 | `step4_band_plot` | S4.1_plot | HSE 能带画图 | band_summary.json |
| 4.2 | `step4_vacuum` | S4.2_vac | ★ HSE 真空能级对齐 | vacuum_align_summary.json |

- **带隙层级由 step.conf 的 `BANDGAP` 参数控制**：`pbe` = 只到 PBE 带隙（1,2,3+3.1/3.2），跳过整段 HSE；`hse`（默认）= 继续 step4+4.1/4.2。
- 可选步骤组开关（默认全开）：`plot_steps: false`、`vacuum_align: false`、`bandgap_hse: false`。
- **作业内三段弛豫**（`STAGE_MODE=in_job`，默认）：一次排队，段间 `cp CONTCAR POSCAR` 接力，某段收敛即跳过后续段（EARLY_EXIT），断点续跑靠 `.sN.done` 标记，OUTCAR 停滞 `STALL_MIN` 分钟看门狗判卡死。想回到旧 tf 级三段（每段一次排队 + `relax_skip` 空转诊断）→ `STAGE_MODE=tf_stages`；单段 → `STAGE_MODE=single`。
- 2D/3D/0D 自动判定；0D（分子）支持到 step1（`MOL_BRANCH=True`，`incar_0d.tpl`），step3/4 对分子无定义、不该跑。
- `aux_files` 随 gen 推送 4 个 `stepN_check_and_resubmit.py`（agent 诊断用）：只允许加 `--check-only` 运行（退出码 0=converged / 10=not_converged / 20=running / 30=重启超限 / 40=error），**重投一律用 tf retry/rerun**。
- 已全量迁移到 `skill_subdir` 布局（`材料/band-dft-cpu/`）；旧平铺目录用 `migrate-subdir` 迁移，手工搬乱了的用 `adopt` 接管（历史迁移，新项目无需关心）。
- 参数（step.conf）：`FUNC=pbesol|pbe|pbe-d3|auto`、`KSPACING`、`BANDGAP`、`MOL_*`（0D）、`STALL_MINUTES` 等。

### 6.2 elastic-dft-cpu 弹性常数（v1.2）

| 步骤 | label | 内容 | 判据 |
|---|---|---|---|
| `step1_std_opt` | S1_opt | 标准结构优化（pymatgen 标准化；2D 真空轴不在 c 时**自动 3-轮换到 c**；2D 面内约束；磁性/LMAXMIX/U 自动判定；作业内分段） | `relax_injob` |
| `step2_elastic` | S2_elastic | IBRION=6 / NFREE=4 有限形变（应力-应变法）；ISYM 自动 | OUTCAR 含 `TOTAL ELASTIC MODULI` |
| `step3_postprocess` | S3_post | 登录节点后处理（不交 SLURM）：Cij 解析、Born 判据、2D→N/m 换算（C×L×0.1）、各向异性图 → `mechanical_properties.json` | done_marker |

- 部署：`tf -tt elastic-dft-cpu init`（在本地材料目录执行，一键追加配置段）或新材料直接 `tf -tt elastic-dft-cpu -p <材料> init`。
- 超算登录节点需要 pymatgen（`pip install --user pymatgen`，2026 年起自动带出核心包 pymatgen-core）与 matplotlib。Born 判据不稳定按"科学结果"处理：文件照出，状态显示 error 提醒人工看。
- 同一材料挂多个技能 = 项目配置里写多段（`band-dft-cpu:` + `elastic-dft-cpu:`），目录互不干扰（默认 `skill_subdir: true`）。

### 6.3 ke-dft-cpu 电子热导率（v0.1，AMSET）

16 步 DAG（`needs` 显式依赖，没写 needs 的步骤回退为"上一步"）：

| seq | 步骤 | label | 内容 | 判据 |
|---|---|---|---|---|
| 1 | `step1_opt` | S1_opt | 结构优化（复用 relax_common，standard 胞） | `relax_injob` |
| 2.1–2.35 | `step2_bandgap/{step2.1_static, 2.2_pbe, 2.2_pbe_plot, 2.3_hse, 2.3_hse_plot}` | S2.1_scf…S2.3_hseplot（合并 S2_bandgap 一列） | 带隙段：static → PBE 能带+画图 →（BANDGAP=hse 时）HSE 能带+画图；`BANDGAP=pbe|hse`（默认 hse） | outcar / wavecar / plot |
| 3 | `step3_uniform` | S3_uniform | 密网格自洽（VASPKIT k 网格） | `wavecar` |
| 4 | `step4_wave` | S4_wave | amset wave | `wavefunction.h5:` |
| 5 | `step5_dielect` | S5_dielect | DFPT 介电常数 | OUTCAR `MACROSCOPIC STATIC DIELECTRIC TENSOR` |
| 6 | `step6_elastic` | S6_elastic | 弹性常数（复用 elastic 脚本） | OUTCAR `TOTAL ELASTIC MODULI` |
| 7 | `step7_deform` | S7_deform | 形变势（**fanout** `*deform*`） | `outcar` |
| 7.1 | `step7b_deform_read` | S7.1_read | 形变势读取（登录节点）→ `deformation.h5` | plot |
| 8 | `step8_amset` | S8_kappa | AMSET 电子热导率 → `transport.json`（needs: wave+dielect+elastic+deform+带隙画图） | `transport.json:thermal_conductivity` |
| 8.2 | `step8.2_dpt` | S8.2_dpt | DPT 形变势迁移率 → `dpt_result.json`（必须排在 8.1 前） | plot |
| 8.1 | `step8.1_boltztrap` | S8.1_bt2 | BoltzTraP2 CRTA × DPT-τ 文献口径完整实现 | `boltztrap_crta.json` |
| 8.3 | `step8.3_output` | S8.3_cmp | AMSET / CRTA×DPT 两口径对比图 | `comparison_300K.png` |

- 开关：`bandgap_steps: false` 完全不算带隙（手填 setting.yaml 的 bandgap）、`bandgap_hse: false`、`dpt: false`、`boltztrap_crta: false`、`output_compare: false`。
- 依赖：`pymatgen, numpy, matplotlib, amset, BoltzTraP2` + `vaspkit`（conda 环境 `amset_clean`）。

### 6.4 kl-dft-cpu 晶格热导率（v0.2，VASP）

| seq | 步骤 | label | 内容 | 判据 |
|---|---|---|---|---|
| 1 | `step1_std_opt` | S1_opt | 结构优化（relax_common，standard 胞） | `relax_injob` |
| 2 | `step2_static` | S2_static | 静态自洽 | `outcar` |
| 3 | `step3_nac` | S3_nac | DFPT 计算 Born 有效电荷/介电常数（**产出 BORN**，kl-mace 接 NAC 用） | OUTCAR 判据 |
| 4 | `step4_disp` | S4_disp | 有限位移（thirdorder，**fanout** `disp-*`） | `outcar` |
| 5 | `step5_fc` | S5_fc | 拟合 fc2/fc3 → 声子谱 + 虚频闸（登录节点） | `phonon` |
| 5.1 | `step5_phonon_plot` | S5.1_plot | 声子谱画图 | plot |
| 6 | `step6_kappa` | S6_kappa | phono3py BTE → κ（`needs: [step5_fc]`） | `kappa.dat:END` |

### 6.5 opt-dft-cpu 结构优化 + 能量（v0.1）

| 步骤 | label | 内容 | 判据 |
|---|---|---|---|
| `step1_opt` | S1_opt | 结构弛豫（复用 relax_common，作业内分段，0D/2D/3D 自动判定） | `relax_injob` |
| `step2_static` | S2_static | 静态自洽（读 S1 CONTCAR 接力，输出总能 E_tot） | `outcar` |
| `step3_energy` | S3_energy | 登录节点后处理：E_tot + 组分 + 参考值 → `energy_summary.json` | done_marker |

S3 产出三个相对稳定性量：

| 量 | 公式 | 要不要参考值 |
|---|---|---|
| `E_per_atom_eV` | E_tot / N_atoms | **不要**（同一组成下直接排名，越低越稳） |
| `E_form_eV` / `E_form_per_atom_eV` | E_tot − Σ n_i·μ_i | 要（`MU`） |
| `E_embed_eV` / `E_embed_per_guest_eV` | E_tot − E_host − n_g·μ_g | 要（`GUEST_ELEMENT`/`HOST_ENERGY`/`MU_GUEST`） |

```bash
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set MU="C:-9.0 Li:-1.9"
tf -tt opt-dft-cpu -p <材料> -j 3 conf --set GUEST_ELEMENT=Li
```

参考化学势 μ_i 取**同一参考态**（如石墨 C、bcc Li）用同一套设置单独算每原子总能填进来；不填也不报错（`E_per_atom` 照常输出）。最严格的判据是凸包上方能量（E_hull），本技能只算形成能（E_hull 的原料），凸包比对用 pymatgen/MP API 离线做。

### 6.6 kl-mace-cpu / kl-mace-gpu 晶格热导率（MACE，4 步）

**MACE 势取代 VASP 算力**：没有 VASP/POTCAR/KPOINTS/ENCUT，没有位移扇出（N 个位移在一个作业里循环算完）。引擎（`mace_relax.py` / `mace_forces.py` / `mace_model.py` / `klmace_common.py`）在公共池 `skill/_common/mace/`，CPU/GPU 两版共用；技能目录里只有 `skill.yaml` + `templates/`，两版差异全在模板（队列、`DEVICE`、超胞默认值）。

| 步骤 | label | 内容 | 判据 |
|---|---|---|---|
| `step1_mace_relax` | S1_relax | MACE 弛豫原胞（ASE + FrechetCellFilter + FixSymmetry），登录节点 | `relax_summary.json` 的 `"converged": true` |
| `step2_disp_force` | S2_force | 位移生成 + **一个作业算完全部力**，计算节点 ×1 | `forces_summary.json` 的 `FORCES_DONE` |
| `step3_fc` | S3_fc | 拟合 fc2/fc3 → 声子谱 → 虚频闸，登录节点 | `phonon_summary.json` 的 `"stable": true` |
| `step4_kappa` | S4_kappa | phono3py BTE → 晶格热导率（**两版都跑 CPU 队列**） | `kappa_summary.json` 的 `KAPPA_DONE` |

**三件必须知道的事**（详见 `skill/_common/mace/README.md`）：

1. **结构一定要用同一个势重弛豫**——力常数是在势自身的能量极小点上做泰勒展开；在 DFT 极小点直接取 MACE 力，残余力混进二阶力常数，Γ 点声学支直接假虚频。`RESIDUAL_TOL`（默认 2e-3 eV/Å）就是这道闸；S2 还会再测未位移超胞的残余力并扣掉。
2. **MACE 给不出 Born 有效电荷和 ε∞**（势里没有电荷响应，原理性缺失）——极性材料不加 NAC 会缺 LO-TO 劈裂、高温 κ 偏。用 `kl-dft-cpu` 跑过 DFPT 的接 BORN：`tf -tt kl-mace-cpu -p <材料> -j step3_fc conf --set params.NAC_BORN=/public/home/.../kl-dft-cpu/step3_nac/BORN`。
3. **`DTYPE` 必须 float64**——float32 的力误差（~1e-3 eV/Å）足以在声学支上造出假虚频。

关键参数（step.conf）：`MACE_MODEL` / `MACE_MODEL_DIR` / `DEVICE` / `CONDA_ENV`（jzzn 上是 venv，写路径即按 venv 激活）、`METHOD=random`（默认 MC-rattle 随机位移）/ `findiff`（有限位移）、`N_RANDOM=auto`（按 ALM 数出的自由力常数反推帧数）、`FC2_SUPERCELL`、`MIN_SC_LEN`、`KAPPA_MESH`、`CKPT`（断点续算）、`ALM_CUT3`、`FIT_SOFTWARE=phono3py`/`pheasy`。GPU 版跑 `opt-mace-gpu` 同款 GPU 机器（`tf hpc` 切）。

### 6.7 opt-mace-cpu / opt-mace-gpu 结构优化 + 形成能（MACE，3 步）

与 opt-dft-cpu 同一套三步骨架，但力全由 MACE 势给出。两技能步骤一致，唯一差别是计算资源：`opt-mace-cpu` 跑 jzzn cpu192 分区（venv `mace_cpu`，`DEVICE=cpu`）；`opt-mace-gpu` 默认 3090 GPU 服务器（conda `mace-gpu`，`DEVICE=cuda`）。

| 步骤 | label | 内容 | 判据 |
|---|---|---|---|
| `step1_mace_relax` | S1_relax | MACE 弛豫（ASE + FrechetCellFilter + FixSymmetry），计算节点作业（submit 模式） | `relax_summary.json` 的 `"converged": true` |
| `step2_mace_static` | S2_static | MACE 静态单点：读 S1 CONTCAR 取总能 E_tot | `static_summary.json` 的 `"STATIC_DONE": true` |
| `step3_formation` | S3_energy | 形成能后处理（登录节点）：E_form = E_tot − Σ n_i·μ_i | `energy_summary.json` |

参数（step.conf）：`MACE_MODEL` / `MACE_MODEL_DIR` / `DEVICE` / `DTYPE` / `CONDA_ENV`、`MU`（如 `C:-9.1757 Mg:-1.5070`）、`FMAX`（出厂默认 1e-3 eV/Å；C60/C120 大体系别用 1e-4，会拖到几小时）、`RELAX_CELL`（发散体系可设 false 锁晶格）。

**参考化学势可以本地算**：`scripts/mace_mu/` 提供固化脚本（38 个金属小胞，本地 CPU 几分钟，不走排队）：

```bash
bash scripts/mace_mu/setup_local.sh   # 第一次：建 venv（torch CPU + mace-torch + ase）
bash scripts/mace_mu/run_mu.sh        # 产出 results.json + 一行 MU= 可直接粘进 step.conf
```

**形成能参考态提醒**：C 用石墨、Mg 用 hcp。富勒烯笼本身比亚稳态石墨高能（曲率应变罚：C60 ≈ +0.4 eV/atom，笼越小越贵），所以这类材料的 E_form 全为正。若要单独看"插层是否有利"，用同一笼的差值 `E_form(CₙMgₘ) − E_form(Cₙ)`，会自动抵消笼的曲率罚——需另建纯笼（Mg=0）材料跑一遍拿 E(Cₙ)。

### 6.8 phonon-mace-cpu 声子谱（MACE，仅 2 阶，3 步）

从 klmace 拆出：只算声子，不碰 fc3 / BTE。

| 步骤 | label | 内容 | 判据 |
|---|---|---|---|
| `step1_mace_relax` | S1_relax | MACE 弛豫（同 klmace） | `relax_summary.json` 的 `"converged": true` |
| `step2_disp_force` | S2_force | 随机位移（帧数由 ALM 2 阶自由力常数反推，振幅 0.01 Å）+ MACE 取力 | `forces_summary.json` 的 `FORCES_DONE` |
| `step3_phonon` | S3_phonon | symfc 拟合 fc2 → q-mesh 虚频闸 + band-dft-cpu.yaml | `phonon_summary.json` 的 `"PHONON_DONE": true` |

### 6.9 mlff-mace 随机位移法 MLFF 训练（v0.1，★ 新技能）

**只做一件事：产出一个经过验证的 MACE 势函数权重文件**（`<MACE_MODEL_DIR>/<材料>_ft.model` + `model_card.json` + `results_<材料>.txt` + 一行可直接填进 kl-mace/phonon-mace 的 `MACE_MODEL` 值）。不做 κ 生产计算、fc3 生产拟合、生产 MD。完整细节见 `skill/mlff-mace/README.md`（出处：autoplex phonon workflow，JCP 153,044104(2020) / Nat. Commun. 16,7666(2025)；数值冲突以 README 为准）。

核心论断：用 phonopy 单原子位移超胞 + 同一原胞生成的一组**随机位移（rattle）超胞**建训练库，**全程不需要分子动力学**。

**九步流水线**：

| seq | 步骤 | label | 在哪跑 | 干什么 | 判据 |
|---|---|---|---|---|---|
| 1 | `step1_relax` | S1_relax | 计算节点（VASP 12 核） | 原胞紧弛豫（三段式 in_job，`EDIFFG=-0.001`，末次 external pressure ≤ 2 kB，2D 只看面内）；读带隙（定 ISMEAR）与磁矩（定 ISPIN/MAGMOM） | `relax_injob`（严判据） |
| 2 | `step2_supercell` | S2_cell | 登录节点 run:gen | 判维度；从基座 `.model` 读 `r_max`（不写死）；每方向 ≥ 2·r_max 定超胞（2D 真空方向恒 1，真空 ≥ max(15 Å, 2·r_max)）；校验基座覆盖全部元素 | `supercell_summary.json` |
| 3 | `step3_calib` | S3_calib | 登录节点 run:gen | 基座模型算 `CALIB_FC2` → u_rms(300K) → `RATTLE_STD=[0.5,1.0,1.6]×u_rms` 三档（虚频则退化 fallback 0.03/0.06/0.10 Å） | `calib_summary.json` |
| 4 | `step4_genstruct` | S4_gen | 登录节点 run:gen | 停机守卫；生成本代待标注构型：rattle 网格（默认 3 应变 × 3 幅度 × 2 种子 = 18 帧）+ 孤立原子（仅第 0 代）+ 单原子位移集（仅第 0 代且无 REF_FC2_PATH）+ static EOS 帧；写 `struct_manifest.json` | `struct_manifest.json` |
| 5 | `step5_label` | S5_label | 计算节点，**fanout** `cfg-*`（12 核/帧） | DFT 单点标注。**唯一昂贵的一步** | `label`（逐子目录） |
| 6 | `step6_dataset` | S6_data | 登录节点 run:gen | OUTCAR → extxyz；指纹校验；extend 并入 + 覆盖分析；离群过滤（过滤帧不删，标 `filtered: true`）；FPS 排序；固定测试集（cfg id 哈希 ~10%，跨代不分） | `dataset_summary.json` |
| 7 | `step7_finetune` | S7_ft | GPU/CPU，**fanout** `seed-*` | MACE 多头微调，N_COMMITTEE=4 个 seed，全量数据 | `finetune_summary.json` |
| 8 | `step8_benchmark` | S8_bench | GPU/CPU（sbatch） | 全部验收闸 + 学习曲线 + 决策表 + 停机判定 + 5 张图 + `results_<材料>.txt` + 追加 `convergence_history.json` | `validation_summary.json` |
| 9 | `step9_publish` | S9_pub | 登录节点 run:gen | 仅当 `status=="pass"` 才拷 `.model` 进 MACE_MODEL_DIR + 写 `model_card.json` | done_marker |

**★ step5_label 操作定则（必须遵守）**：扇出步骤、整条链唯一花大钱的地方——**只用 `retry` 或 `start -f`，绝不用 `rerun` 和 `clean`**（后两者会 `rm -rf` 步骤目录，毁掉已算完的 DFT 帧）。`retry` 只补没完成的帧；单独补某几帧就进对应 `cfg-*` 目录手工 sbatch。

**代数迭代（taskflow 是 DAG，代数用 step.conf 的 GENERATION 表达）**：

```bash
tf -tt mlff-mace -p <材料> conf --set params.GENERATION=1
tf -tt mlff-mace -p <材料> -j 4 rerun    # 只删 step4 结构清单重生成（安全）
tf -tt mlff-mace -p <材料> start         # 判据检测到「代数不一致」→ 5/6/7/8 自动补生成/重跑/提交
```

**验收闸（step8，10 条，写入 validation_summary.json）**：① 声子谱 RMSE < 0.2 THz（主收敛闸，autoplex 判据）；② imagmodes(pot)==imagmodes(dft)（3D 阈 −0.1 THz）；②b（仅 2D）ZA 支 |q|<0.05|b| 内最低频率 ≥ −0.05 THz；③ 平衡结构残余力 < 1e-3 eV/Å 且空间群不变（**必须用微调后势自身重弛豫再取力**）；④ ASR 违反 < 1e-3 eV/Å²；⑤ 测试集力 RMSE < 40 meV/Å；⑥ 能量 RMSE < 3 meV/atom；⑦ 弛豫晶格常数偏差 < 1%；⑧ EOS/模量偏差 < 5%（3D 体模量 / 2D 面内二维模量）；⑨ committee σ_F 外推率 < 5%；⑩ 模式 Grüneisen γ MAE < 0.3（验收唯一的三阶敏感量，但**fc3 与 κ 的最终可信度由下游 kl-mace-* 背书，本技能不替它背书**）。

**停机规则（硬性生效）**：Δ_K = rmse(K−1) − rmse(K) < IMPROVE_MIN(0.02 THz) 且主闸未过 → 本代 stagnant；**连续两代 stagnant → halt_stagnant FAIL**；学习曲线已平且主闸未过 → halt_not_data_limited（附六条排查清单）。`GENERATION > MAX_GENERATION` 硬停；停机后 gen 拒绝推进，除非显式 `FORCE_CONTINUE=true`。无论哪种停机都**保留全部数据和最后一代模型**，model_card 如实写 `"converged": false`。

**关键参数（step.conf）**：`FUNC / DIMENSION / ENCUT_OVERRIDE / DATA_MODE(scratch|extend) / PRE_XYZ_FILES / REF_FC2_PATH / VOL_FACTORS / RATTLE_STD / N_PER_CELL / MIN_DIST_RATIO / REF_DISP / ISO_BOX / MIN_ATOMS / MAX_ATOMS / MIN_VACUUM / GENERATION / MAX_GENERATION / RMS_MAX / IMPROVE_MIN / GEN_INCREMENT / CURVE_POINTS / CURVE_TOL / FORCE_CONTINUE / ENERGY_LIMIT / FORCE_LIMIT / KSPACING_TOL / MACE_MODEL / MACE_MODEL_DIR / REPLAY_XYZ / N_COMMITTEE / ENERGY_WEIGHT / FORCES_WEIGHT / STRESS_WEIGHT / BATCH_SIZE / DTYPE / LR / EPOCHS / DEVICE / CONDA_SH / CONDA_ENV`（出厂默认见 `templates/step.conf`）。死规则：`DTYPE=float64` 不许改；`REPLAY_XYZ` 必需（集群无外网，拿不到就报错退出、不做 naive 微调降级）；多头不是可选项。

**与 autoplex 默认值的重要差异**（全表见 README §1）：① 基准超胞 = 训练超胞（不烧 min_length=20 大胞，代价是 commensurate q 分辨率较粗）；② `REF_DISP=0.1 Å`（= autoplex 默认；实测 0.01 Å 的位移力被 rattle 帧淹没约 1000 倍，微调后光学支软 27%、声子 RMSE 2.8 THz——CALIB_FC2 标定仍用 0.01 Å）；③ 微调超参取 autoplex `_mace_hypers.py` 默认（lr=1e-3、EPOCHS=1500+patience 早停+SWA、loss=huber、batch=10、stress_weight=1.0，multihead 需 `--force_mh_ft_lr`）；④ `FORCE_LIMIT=40.0 eV/Å`（= autoplex force_max，0.1 会把大幅度 rattle 帧全滤掉）；⑤ 默认 `E0S_MODE=estimated`（基座 E0 与目标泛函零点差 ~8 eV/atom，直接塞 DFT 孤立原子能量会训不动）。

**环境与现状**：jzzn 登录节点无外网、无 GPU 分区（DEVICE=auto 恒落 CPU，EPOCHS=1500 上限 + PATIENCE=100 早停在 CPU 上单 seed 约 1~3 小时）；GPU 路径（a800/3090 的 `submit_mace.tpl` + CONDA_ENV）已预留、未实测。实测记录：Si 金刚石原胞 → 超胞 4×4×4（128 原子，r_max=6.0 Å 读自基座），第 0 代 25 帧（18 rattle + 3 displ + 3 static + 1 iso）×12 核，S1~S6 通过，S7 4-seed 微调进行中（截至本文更新）。

---

## 7. 技能开发规范（原 SKILL_DEV.md 内容）

> 本章即原独立的 `SKILL_DEV.md`，内容已并入本文。照本章写出的技能目录，放进 `skill/` 即可被 `tf` 自动发现，**不需要修改 tf 主程序的任何一行**。本章可以整份喂给 AI 让它生成技能（7.13 有现成提示词模板）。

### 7.1 心智模型：谁负责什么

```
本地（你的机器）                          超算（登录节点 + 计算节点）
┌──────────────────────────┐             ┌──────────────────────────────┐
│ tf 主程序                 │             │                              │
│  · 读 skill.yaml 装配流水线 │  ssh 推送   │ 材料目录/                     │
│  · 决定「下一步该干什么」    │ ─────────► │   ├── POSCAR                 │
│  · 提交 / 取消 / 重跑       │            │   ├── gen_stepN_xxx.py       │
│  · 拉回结果                │            │   ├── dim_common.py, *.tpl   │
│                          │            │   └── stepN_xxx/             │
│ skill/<技能>/             │            │        ├── INCAR KPOINTS     │
│  ├ skill.yaml  ← 流水线声明 │            │        ├── POTCAR POSCAR     │
│  ├ gen_*.py    ← 造输入     │            │        ├── submit.sh         │
│  ├ checks.py   ← 判完成     │  ssh 执行   │        └── OUTCAR ...        │
│  └ *.tpl       ← INCAR/提交 │ ─────────► │                              │
└──────────────────────────┘             └──────────────────────────────┘
```

三件事必须分清：

| 角色 | 在哪跑 | 干什么 |
|---|---|---|
| `skill.yaml` | 本地被 tf 解析 | **声明**有哪些步骤、每步用哪个 gen 脚本、用什么判据判完成 |
| `gen_*.py` | 超算登录节点，cwd = **材料目录** | **造**出 `<步骤名>/` 目录及其中的 INCAR/KPOINTS/POTCAR/POSCAR/submit.sh |
| 判据（`check:`） | 超算登录节点，在 tf 下发的采集器里 | **判断**某个步骤目录算完没有、结果对不对 |

tf 自己**不懂任何物理**。它只会：建目录 → 推文件 → 跑 gen → sbatch → 按判据看状态 → 拉结果。所有 VASP/MACE 知识都在技能里。

### 7.2 目录结构与公共池

```
skill/<技能名>/
├── skill.yaml                 必需。技能清单
├── gen_step1_xxx.py           必需。每个计算步骤一个（也可一个脚本 --stage 复用）
├── checks.py                  可选。本技能私有的完成判据
├── stepN_check_and_resubmit.py   可选。每个计算步骤一个（agent 诊断用）
├── templates/                 模板（template_layout: shared 或 per_step，见下）
└── README.md                  可选。给人看的说明

skill/_common/                 公共池（没有 skill.yaml，不会被当成技能）
├── dim_common.py  stepconf.py  check_common.py  mol_common.py  relax_common.py
├── templates/incar_0d.tpl     0D 共用模板
├── mace/                      kl-mace-gpu / kl-mace-cpu 共用引擎
└── opt/                       结构优化公共件
```

**公共池规则（当前约定）**：与超算无关的公共库（`dim_common.py`、`stepconf.py`、`check_common.py`、`relax_common.py` 等）放 `skill/_common/` **只写一份，所有技能共用**；改公共池 = 所有技能一起改。技能目录里有同名文件就优先用自己的（想给某一版彻底自包含：把池子里的文件 `cp` 进技能目录即可，副本优先；删掉副本就回到共用）。`gen_need` 清单写文件名就行，变的是 tf 去哪里找。**技能之间不许跨目录 import**（`import ../别的技能/xxx` 是不允许的）——要复用就依赖公共池或拷副本。

技能名 = 目录名 = `-tt` 的短名（如 `tf -tt kl-dft-cpu start`）。用小写字母、数字、连字符。

### 7.3 模板放哪：`template_layout`

模板文件（`*.tpl`）可以摊在技能根目录下（默认，向后兼容），也可以收进 `templates/`。清单里用 `template_layout` 选：

- **`shared`（缺省）**——所有步骤共用一套模板：`templates/incar_2d.tpl` 等。
- **`per_step`**——每个步骤一个目录：`templates/<步骤名>/...`，子目录名 = `steps[].name` 必须逐字相同；`templates/` 根作公共回落。

查找顺序（技能目录内，末尾平铺兜底）：

| 布局 | 顺序 |
|---|---|
| `shared` | `templates/<文件>` → `<技能根>/<文件>` |
| `per_step` | `templates/<步骤名>/<文件>` → `templates/<文件>` → `<技能根>/<文件>` |

> ⚠️ `per_step` 布局下 `tf init` **不会**把模板复制进 `project_setting/`（那里一份会盖住所有步骤）。要按项目改某步的模板，放 `材料/<技能>/templates/<步骤名>/` 下。

只放**实际用得到**的文件（全程 `vasp_std` 就不放 `submit_ncl_*.tpl`；纯 3D 技能不放 `incar_2d.tpl`），不用的模板放进来只会让 `tf init` 报无意义的警告。

**提交模板只写逻辑名**（`submit_std_2d.tpl`、`submit_std_3d.tpl`、`submit_mace.tpl`、`submit_amset.tpl`…），实际文件放在 `setting/<hpc>/templates/`（逻辑名即文件名，按超算分文件夹；`<步骤名>/` 子目录放该步骤变体）。技能目录**不写死** `submit_jzzn_*`，换超算不用改技能。

**依赖清单写在每个步骤上**（步骤级 `gen_need`），不要写在类型顶层——每一步只推送自己真正需要的文件。⚠️ **步骤级 `gen_need` 会完全替代类型级清单，并且跳过提交模板的自动补推**：每一步都必须把提交模板逻辑名列全，漏写时老材料靠远端残留文件掩盖，新材料（空目录）会直接报「找不到模板」。

### 7.4 `skill.yaml` 完整字段

```yaml
schema: 1                  # 必需。清单格式版本，当前固定 1
name: kl-dft-cpu                   # 可选。类型 key，缺省 = 目录名
desc: 晶格热导率            # 必需。状态表和帮助里显示的中文名
version: "0.1"             # 可选。技能自身版本，tf skills 会显示
enabled: true              # 可选。false = 不装载（默认 true）

defaults:                  # 可选。站点相关缺省值，用户在 tf.yaml 里覆盖
  hpc: jzzn                #   默认集群（对应 setting/<name>.yaml）
  skill_subdir: true       #   true = 材料目录下建 <技能名>/ 子目录（新技能一律 true）
  # work_dir: ...          #   一般不写，让用户在 tf.yaml 里配

gen_need: [...]            # 可选。类型级依赖文件（gen 前推到材料目录，已存在按 md5 比对）
aux_files: [...]           # 可选。辅助脚本（同上，只补不覆盖）
fetch_files: [...]         # 可选。fetch 拉回清单（小文件；大产物如 *.hdf5 不拉）
checks: checks.py          # 可选。私有判据文件名（可指向公共池 ../_common/checks_relax.py）

steps:                     # 必需。顺序即流水线顺序
  - {seq: 1, name: step1_relax, label: S1_opt, check: relax_injob,
     gen: "lattice_kappa.py --stage relax", contcar_to_poscar: true}

optional_steps:            # 可选。可开关的步骤组，见 7.8
  plot_steps:
    default: true
    steps: [...]

requires:                  # 可选。人读为主
  python: [numpy, phonopy]
  conda: mace
  exe: [ShengBTE]
```

### 7.5 `steps[]` 每一条的字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `name` | ✅ | **步骤目录名**，必须和 gen 脚本实际创建的目录**一模一样** |
| `label` | ✅ | 状态表列头，≤ 10 字符，形如 `S2_static` |
| `seq` | 推荐 | `run_steps` / `-j` 用的序号。多段合并成一步就共用同一个 seq；画图步用小数 `3.1` |
| `check` | ✅ | 完成判据名，见 7.6 |
| `gen` | ✅ | gen 脚本名，可带参数：`"gen_x.py --stage a"`。可用占位符 `{mat} {matdir} {root} {step} {tt}` |
| `gen_need` | | **步骤级**依赖清单。写了就**完全替代**类型级 `gen_need`+`aux_files`，并跳过提交模板自动补推 |
| `run` | | `gen` = 只在登录节点跑 gen 脚本、**不提交 SLURM**（后处理/画图步用） |
| `group` | | 多个步骤在状态表合并成一列（如带隙子步都写 `group: S2_bandgap`） |
| `needs` | | **显式 DAG 依赖**：本步要等列出的步骤全 OK 才启动；没写则回退为"上一步"（ke-dft-cpu/kl-dft-cpu 用） |
| `src` | | 源目录字段：嵌套步骤（`step2_bandgap/step2.1_static`）从技能目录的哪个子目录取脚本/模板 |
| `contcar_to_poscar` | | `true` = `retry` 续跑前先把 CONTCAR 盖回 POSCAR（弛豫步用） |
| `submit` | | 提交脚本文件名，缺省 `submit.sh`（tf 也会兜底找 `sub.sh/job.sh/run.sh/sub.slurm`） |
| `fanout` | | 扇出步骤：步骤目录下每个匹配子目录是一个独立作业，见 7.9。值是 glob，如 `"cfg-*"` |
| `hpc` | | **本步骤指定超算**（v1.12）：写集群名（如 `hpc: jzzn`）则本步骤的采集/状态检查/提交/模板都走该集群，材料其余步骤留在默认集群。见 5.4.1 |
| `fetch_all` | | `true` = 完成后整目录拉回本地 `result/`（画图/后处理步用） |
| `marker` / `done_marker` / `phrase` / `pressure_tol` / `stage` / `relax_diag` / … | | 判据参数，见 7.6 |
| 任意自定义键 | | **会原样传给判据函数的 `sc`**，这是自定义判据取参数的方式 |

> ⚠️ `name` 必须和 gen 脚本里写的目录常量一致。这是 90% 的「步骤永远 PREP」问题的根因。

### 7.6 内置判据（`check:` 可选值）

| 判据 | 判定逻辑 | 可调参数（写在该 step 里） |
|---|---|---|
| `outcar` | OUTCAR 尾部含 `General timing and accounting informations` | — |
| `outcar_relax` | 上面 + 含 `reached required accuracy` + 末次 `external pressure` 绝对值 ≤ 阈值；不过时附弛豫空转诊断 | `phrase`（默认 `reached required accuracy`）、`pressure_tol`（默认 5.0 kB） |
| `relax_injob` | **作业内分段弛豫判据**（公共池 `../_common/checks_relax.py`）：OUTCAR 总闸收敛；未收敛时从 `.sN.done` 数进度并区分"跑完没收敛 / 某段中断"；兼容老 `step1{a,b,c}_*` 目录 | `pressure_tol`、`stage` |
| `relax_skip` | 收敛感知的 tf 级多段弛豫判据（旧 `STAGE_MODE=tf_stages`）：a 收敛则 b/c 自动跳过，c 是总闸 | `stage`（`a`/`b`/`c`）、`relax_diag` |
| `wavecar` | WAVECAR 存在且 ≥ 阈值 | `wavecar_min`（默认 1 MB） |
| `eigenval` | EIGENVAL 存在；有 KPOINTS_OPT 时还要有 vasprun.xml | — |
| `marker` | **通用判据**：`marker: "文件名:要找的字符串"` | `marker`（必填） |
| `plot` | `done_marker` 指定的文件存在，或目录里有任意 `.png` | `done_marker` |
| `phonon` | 公共池私有判据：声子谱/虚频闸（kl-mace step3） | — |

**先用 `marker`**。绝大多数「某文件里出现某行就算完」的需求都不用写代码：

```yaml
- {name: step2_elastic, check: marker, marker: "OUTCAR:TOTAL ELASTIC MODULI", ...}
- {name: step3_fc2,     check: marker, marker: "FORCE_CONSTANTS_2ND:",         ...}
```

`marker` 不够用（要读数值、要比较、要跨文件）才写 `checks.py`。

**弛豫空转诊断 `relax_diag`**（`outcar_relax` / `relax_skip` 未收敛时读 OSZICAR）：`progressing`（正常下降）/ `oscillating`（末段振荡）/ `thrown`（线搜索甩飞）/ `electronic`（撞 NELM）/ `stalled`（停滞）/ `nsw`（步数用完）。默认参数 `{window: 8, osc_tol: 5e-3, stall_tol: 1e-4, jump_tol: 0.5, min_steps: 6}`，某步想改就写 `relax_diag: {window: 10, osc_tol: 0.003}`。注意：`in_job` 模式下段间闸门由 bash 看门狗（`STALL_MIN` 卡死判定）承担，没有这套细判——排队时间远大于单段机时的集群上这是划算的，反复震荡的体系可临时切回 `tf_stages`。

### 7.7 `gen_*.py` 契约

**运行环境**：超算登录节点，`cwd = 材料目录`，命令是 `python <脚本名> <你在 gen: 里写的参数>`。

**必须做的事**：

1. 创建 `<步骤名>/` 目录（名字必须等于 `skill.yaml` 里的 `name`）。
2. 在其中生成输入文件：`POSCAR`（从上一步 CONTCAR 接力）、`INCAR`、`KPOINTS`、`POTCAR`、`submit.sh` 等。
3. 出错就 `sys.exit("[ERROR] ...")`，非零退出码 → tf 报 gen 失败并把 stderr 原样呈给用户。
4. 结构接力要**显式**：前一步的 CONTCAR/summary/`.model` 不存在就报错退出，**绝不能拿旧文件默默往下算**。

**可以依赖的东西**（tf 会自动推到材料目录）：

- `gen_need` / `aux_files` 里列的所有文件，与 gen 脚本同目录（`Path(__file__).parent`）
- `dim_common.py` 常用 API：
  - `detect_dimension(poscar, vacuum_min=8.0, allow_0d=None)` → `("0d"|"2d"|"3d", axis, vacuums)`（≥2 个真空方向 = 0d 孤立分子）
  - `resolve_dim(method_file, struct_path)` → 优先继承 `workflow_method.txt` 的 `DIM=`，缺失才现场判定
  - `resolve_tpl(base_dir, "submit_std", dim)` → 按维度选 `<base>_<dim>.tpl`，回退无后缀旧名
  - `validate_poscar(path)`、`force_kz1(kpoints_path)`、`filter_kpath_2d(...)`
- **`relax_common` 薄壳**：结构优化步骤的引擎在公共池。技能的 `gen_step1_*.py` 退化成十几行：

  ```python
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  import relax_common as R
  R.run(OUTDIR_SINGLE="step1_std_opt", SCRIPT_NAME="gen_step1_std_opt.py",
        NEXT_STEP="gen_step2_elastic.py", STAGE_MODE="in_job",
        CELL_POLICY="standard", MOL_BRANCH=False)
  ```

  常用键：`OUTDIR_SINGLE` / `OUTDIR_PATTERN` / `SCRIPT_NAME` / `NEXT_STEP` / `JOBNAME_SUFFIX` / `CELL_POLICY`（`primitive` 能带/声子，`standard` 弹性，`none` 缺陷/分子）/ `STD_CELL` / `MOL_BRANCH`（0D 分支开关）/ `RELAX_STAGES` / `STAGE_MODE`（`in_job` 缺省 / `tf_stages` 旧三段 / `single`）/ `STAGE_SPEC` / `AUTO_MAG` / `AUTO_U` / `MANUAL_ENCUT` / `DIMENSION` / `CELL_CONSTRAINT_2D`…。**键名必须在 `DEFAULTS` 里**，写错立刻报错而不是被静默忽略。改公共池后请跑一次等价性验证（同一 POSCAR 下新旧脚本 `diff -r` 输出目录）。

- **`step.conf` 参数读取**：不要硬编码参数。脚本顶部声明 `CONF_SPEC` 并从 step.conf 读：

  ```python
  CONF_SPEC = {
      "FUNC":     ("pbesol", "str"),
      "KSPACING": ("0.03",   "str"),      # <- 新增参数
  }
  C = stepconf.load(CONF_SPEC, step_name)
  ```

  类型支持 `str` / `int` / `float` / `bool` / `words` / `elemmap`。**`[params]` 里没在 CONF_SPEC 声明过的键会直接报错**；公共池里所有读 step.conf 的模块必须共用一份 `CONF_SPEC`、只解析一次（`relax_common.load_step_params()` 存进 `STEP_PARAMS`，其它模块从那里取）。step.conf 里写了 `STEP=` 时，`stepconf.load` 会校验它与本步名一致。

**模板逻辑名机制**（换超算不用改技能）：技能里只写**逻辑名** `submit_std_2d.tpl`、`submit_std_3d.tpl`、`submit_mace.tpl` 等；实际文件在 `setting/<集群>/templates/`（逻辑名即文件名）。模板里的 `{{JOBNAME}}` 由 gen 脚本替换。

**`run: gen` 的步骤**（后处理/画图）：同样在登录节点跑，但 tf **不提交 SLURM**，跑完就按 `check: plot` + `done_marker` 判完成。所以这类脚本要自己算完并写出产物文件，且不能太重（登录节点跑得动）。

### 7.8 可选步骤组 `optional_steps`

用来表达「这几步默认加上，但可以一键关掉」（能带画图、真空对齐、HSE 分支、DPT 分支就是这么来的）：

```yaml
optional_steps:
  plot_steps:                    # 键名 = 开关名，用户写 plot_steps: false 关闭
    default: true
    steps:
      - {seq: 3.1, name: step3_band_plot, label: S3.1_plot,
         after: step3_PBE,       # 锚点：插在名字以此开头的最后一个步骤之后
         gen: "gen_step3.1_plot_band.py", check: plot, run: gen,
         gen_need: [], done_marker: band_summary.json, fetch_all: true}
```

- `after` 是**步骤名前缀**。锚点在本技能里不存在 → 该条自动不注入（不会报错）。
- 一个技能可以有多个开关组，键名自取；组内步骤可写 `needs` 建组间/组内 DAG。
- 用户在 `tf.yaml` 或项目 `tf_<项目>.yaml` 里写 `<开关名>: false` 即关闭；step.conf 参数也可以驱动开关（如 `BANDGAP=pbe|hse` 映射到 `bandgap_hse` 组的开/关）。
- `run_steps` 是另一个正交机制（用户侧）：`run_steps: [1, 2, 3.1]` 只跑列出的步骤，元素匹配 `seq`、`name` 或 `label`。**所以每个步骤都写 `seq` 很重要**，否则用户只能敲全名。

### 7.9 扇出步骤 `fanout`：一步下面 N 个并行作业

有些计算天然是「同一步骤、N 份独立输入、各跑各的」——形变势的 13 个应变、phono3py 的 N 个位移、mlff-mace 的 DFT 标注帧（`cfg-*`）与多 seed 微调（`seed-*`）。tf 默认「一步 = 一个目录 = 一次 sbatch」，这类步骤用 `fanout` 声明：

```yaml
- {seq: 7, name: step7_deform, label: S7_deform, check: outcar,
   fanout: "deform-*",              # glob，相对步骤目录
   gen: gen_step7_deform.py, fetch_all: true}
```

gen 脚本负责在步骤目录下造出这些子目录，**每个子目录一份完整输入 + 自己的 `submit.sh`**。之后 tf 全自动：

| 操作 | 行为 |
|---|---|
| `start` | 每个匹配子目录各 `sbatch` 一次，作业名自动加子目录后缀 |
| 状态 | 全部子目录判据都过才算 `done`；跑的时候显示 `3/5 2R 0PD` |
| 失败 | 任一子目录没过 → 整步 `error`，诊断列出是哪几个：`3/5 完成；未完成 deform-04,deform-05` |
| `retry` | **只补没完成的那些**，已算好的不动 |
| `stop` | scancel 该步骤的全部作业 |
| `rerun` | 删掉整个步骤目录（含所有子目录）重来 |

判据（`check:`）作用在**每个子目录**上，不是步骤目录。注意事项：

- 子目录名要能被 glob 稳定匹配，且不要和别的东西撞（`deform-*` 而不是 `*`）
- gen 脚本要**幂等**：重跑时已有子目录不要清空已算好的结果
- `fetch_all: true` 会把整个步骤目录（含全部子目录）拉回本地，产物多的步骤建议改用 `fetch_files` 只拉汇总产物
- **昂贵的扇出步骤定操作定则**（如 mlff-mace 的 step5_label）：只用 `retry`/`start -f`，**绝不用 `rerun`/`clean`**——后两者会毁掉已算完的子作业产物

### 7.10 `checks.py` 判据插件契约

只有在 `marker` 判据不够用时才写。**这是整套机制里约束最强的地方，务必逐条遵守**：

```python
# -*- coding: utf-8 -*-
"""skill/<技能>/checks.py"""

def ck_kappa_conv(d, sc):
    """d  = 该步骤在超算上的绝对目录
       sc = 该步骤的配置字典（skill.yaml 里同一条 step 的所有键都在这）
       返回 (是否完成: bool, 诊断文本: str)"""
    p = os.path.join(d, "BTE.KappaTensorVsT_CONV")
    if not os.path.isfile(p):
        return False, "BTE.KappaTensorVsT_CONV missing"
    rows = [ln.split() for ln in tail_text(p, 200000).splitlines() if ln.strip()]
    if not rows:
        return False, "结果文件为空"
    last = [float(x) for x in rows[-1]]
    return True, "kappa@%.0fK = %.2f W/mK" % (last[0], last[1])


CHECKERS = {"kappa_conv": ck_kappa_conv}    # ← 必须有这一行
```

**硬约束**：

1. 这个文件的源码会被 tf 读出来、base64 塞进采集器、在**超算登录节点 `exec` 执行**。
2. **只能用标准库**。不能 `import numpy`、不能 `import pymatgen`。
3. **不能有顶层副作用**：除了 `def` 和 `CHECKERS = {...}`，不要有 print、文件读写、`import` 之外的语句。
4. **不要写 `import os` 等**——采集器已经在全局命名空间提供了：`os` `re` `json` `glob` `subprocess`，以及 `tail_text(path, nbytes=1000000)`（读文件尾部）、`read_oszicar_ionic(d)`（OSZICAR 离子步能量）、`relax_diagnose(d, cfg)`（弛豫空转诊断）。写了 `import os` 也不报错，但没必要。
5. **判据要能在 1 秒内返回**。它对每个材料的每个步骤都要跑一遍，不能扫全文件、不能起子进程算东西。**数值重活留给作业脚本算完写 json，判据只读 json**（mlff-mace 的 benchmark 就是这么分工的）。
6. **判据名不能和内置判据重名**（`outcar`/`marker`/`plot`/…），重名 tf 会直接报错退出。
7. 判据参数从 `sc` 取（`sc.get("kappa_rtol", 0.01)`），参数写在 `skill.yaml` 那条 step 上，tf 会自动透传到远端。

判据可以放公共池：`checks: ../_common/checks_relax.py`（band/elastic/ke/kl 的 `relax_injob` 都在这里）。

### 7.11 用户侧配置（技能作者要知道的）

技能装好后，用户在全局 `tf.yaml` 里只需要：

```yaml
task_types:
  kl-dft-cpu:
    work_dir: /public/home/xxx/work     # 超算工作根，这个必须用户配
    # hpc: a800                         # 覆盖清单里的默认集群
    # plot_steps: false                 # 关掉可选步骤组
    # run_steps: [1, 2]                 # 只跑部分步骤
    # max_jobs: 100                     # 本技能并发提交上限
```

**不要在技能清单里写 `work_dir` 的具体路径**，那是站点信息，留给用户。

同理，**集群相关的环境路径（conda.sh / conda 环境 / amset 环境等）也不要写死在技能或 gen 脚本里**：写在 `setting/<集群名>.yaml` 的 `conda_sh` / `conda_env` / `amset_env`，tf 自动注入每步 step.conf（见 5.4.2），gen 脚本从 step.conf 读。换人换机器只改 `setting/<集群名>.yaml`。

### 7.12 自检清单

写完一个技能，按顺序过这几关：

```bash
# 1. 清单能被解析、技能能被发现
tf skills
#    应看到你的技能，版本、步骤数、清单路径都对

# 2. 步骤表能正确展开（含 optional_steps）
tf -tt <技能名>
#    列头 = 你的 label，顺序 = 你的 steps 顺序

# 3. 挂到一个测试材料上（纯本地，不连超算）
cd <含 POSCAR 的材料的上级目录>
tf -tt <技能名> init

# 4. 只生成第一步输入、不提交，人工检查 INCAR/KPOINTS/POTCAR
tf -tt <技能名> -p <材料> -j 1 init
tf -tt <技能名> -p <材料> dir     # 拿到远端路径，ssh 过去看

# 5. 真跑
tf -tt <技能名> -p <材料> start
```

逐条对照：

- [ ] 依赖清单写在**步骤级** `gen_need`，每一步都列全了提交模板逻辑名（步骤级会完全替代类型级并跳过自动补推）
- [ ] 依赖文件都在本技能目录或公共池里：技能目录里 `ls` 逐条对照；依赖公共池的确认池子有、跨技能引用确认没有
- [ ] `skill.yaml` 的 `schema: 1`、`name`、`desc`、`steps` 齐全；新技能 `defaults.skill_subdir: true`
- [ ] 每个 `steps[].name` 与 gen 脚本创建的目录名**逐字相同**；嵌套步骤（`step2_bandgap/step2.1_static`）`src` 与目录对齐
- [ ] 每个步骤有 `seq`、`label`（≤10 字符）、`check`、`gen`；需要 DAG 的写了 `needs`
- [ ] 判据优先用内置的；`marker` 的 `文件名:字符串` 确认在真实输出里出现过
- [ ] gen 脚本：cwd 是材料目录、结构接力找不到就报错退出、非零退出码有意义；参数走 `step.conf`（CONF_SPEC 声明过）不硬编码
- [ ] 提交模板只写逻辑名 `submit_std_2d.tpl` 等，不写 `submit_jzzn_*`；模板本体放在 `setting/<hpc>/templates/`
- [ ] 后处理/画图步写了 `run: gen` + `check: plot` + `done_marker` + `fetch_all: true`
- [ ] 扇出步骤：gen 脚本造的子目录名与 `fanout` 的 glob 对得上，每个子目录都有自己的 `submit.sh`，gen 幂等
- [ ] 若有 `checks.py`：只用标准库、无顶层副作用、判据名不与内置重名、秒级返回
- [ ] `tf skills` 里没有关于你这个技能的警告

### 7.13 喂给 AI 的提示词模板

把本章整份贴进去，然后追加：

```
上面是 taskflow 的技能开发规范。请按它生成一个新技能，要求如下：

【技能名】     <目录名，如 dielectric>
【中文描述】   <如 DFPT 介电常数计算>
【物理流程】
  1. <第一步做什么，用什么 INCAR 关键字，判完成看什么>
  2. <第二步…>
  3. <…>
【已有素材】
  - <贴出你已有的脚本 / INCAR 模板 / 参考的 skill/band-dft-cpu 里的哪些文件>
【集群】       jzzn，提交模板逻辑名用 submit_std_2d.tpl / submit_std_3d.tpl
【维度】       需要 / 不需要 2D-3D 自动判定（需要就复用公共池 dim_common.py）

请输出：
  1. skill/<技能名>/skill.yaml         —— 完整清单，每步都要有 seq/label/check/gen
  2. skill/<技能名>/gen_step*.py       —— 每个计算步骤一个，遵守 gen 契约
  3. skill/<技能名>/checks.py          —— 仅当内置判据（尤其 marker）不够用时才写，
                                          写了必须遵守 7.10 的全部硬约束
  4. 一份自检清单的逐条核对结果

约束：
  - 不要修改 tf 主程序，不要要求我改 tf.yaml 里除 work_dir 以外的东西
  - 依赖文件要么在本技能目录（自包含），要么引用公共池 skill/_common/（gen_need 写文件名即可），
    不许跨技能目录 import。需要公共池里没有的文件时，明确告诉我「从哪个目录 cp 哪几个文件」
  - 依赖清单写在步骤级 gen_need，每一步都要列全提交模板逻辑名；模板本体放 setting/<hpc>/templates/
  - 判据能用 marker 就用 marker，不要为了「显得完整」去写 checks.py
  - skill.yaml 里不要写具体的 work_dir 路径；defaults 里写 skill_subdir: true
  - gen 脚本不得在上一步 CONTCAR 缺失时静默使用旧结构；参数走 step.conf（CONF_SPEC）
```

### 7.14 常见坑

| 现象 | 原因 |
|---|---|
| 步骤永远 `PREP` | `steps[].name` ≠ gen 脚本创建的目录名 |
| 步骤永远 `FAIL` | 判据字符串在真实输出里根本不出现；先 `grep` 一遍真 OUTCAR 再写 `marker` |
| 新材料报「找不到模板」 | 只写了 `gen_need` 没包含提交模板逻辑名；步骤级 `gen_need` 会完全替代类型级并跳过自动补推，得自己列全 |
| `tf skills` 里技能不出现 | 清单没有 `steps`、`enabled: false`、或被 `tf.yaml` 的 `disabled_skills` 关掉了；带 `-v` 看警告 |
| 自定义判据在本地测好好的，远端报错 | 用了非标准库，或有顶层副作用 |
| 判据拿不到参数 | 参数写在了错误的层级——必须写在 `steps[]` 的那一条里 |
| 状态表列宽爆炸 | `label` 太长，或没用 `group` 合并多段步骤 |
| 改了技能脚本远端没生效 | gen 脚本每次覆盖推送（本地改即生效）；但 `gen_need` 依赖文件按 md5 比对，改了内容会推，只改文件名不会 |
| step.conf 里写个键报错 | 键没在脚本的 `CONF_SPEC` 里声明（这是防"改了没人读的变量"的守卫，不是 bug） |
| step.conf 的 `STEP=` 报错 | 写了与调用方步骤名不一致的 `STEP=`（如项目里残留 `STEP=step1a_PBE_opt`，而 in_job 后步骤名是 `step1_PBE_opt`） |
| 改了公共池没效果 | 技能目录里还留着同名副本（副本优先）；`rm` 副本即回到共用 |
| 换了 hpc 提交模板没变 | 项目 `project_setting/templates/` 里还有旧副本（项目内覆盖优先级最高） |

---

## 8. 工作原理（无状态）

作业与步骤的对应关系来自 `squeue` 的工作目录（%Z），同名作业不混淆；scancel 后状态自动回落为文件判据，无状态残留。每次调用只 ssh 一次，同时采集所有任务类型。三台机器都已装真 SLURM，tf 采集照常工作。

---

## 9. 给大语言模型用（agent 接入）

`tf` 按"LLM 工具"设计，三个接口约定：

1. **命令原子化**：`summary/list/status/start/stop/retry/rerun/json`，参数 `-tt/-p/-j` 语义稳定，适合 agent 调用。
2. **`tf summary`（巡检主输入，省 token）**：只读极简汇总，每类型一行计数 + FAIL 清单；配合 `-status error` 只拉失败的。`tf json` 是**全量结构化状态**（`types → materials → steps`，含 `kind/diag/job/action`），token 巨大，只在写工具/做批量分析时用，巡检禁止每轮拉。
3. **退出码**：0 = 成功；非 0 = 失败或被拒绝（如步骤已有作业未加 `-f`、FAIL 步骤直接 `start`、取消确认被拒）。agent 据此判成败，不用猜文本。

配套文件 **`AGENTS.md`**：给 LLM 的完整接入规范（角色、安全铁律、命令参考、FAIL 诊断决策树、token 省流监控技能、汇报模板）。用法：

- **Kimi Claw**：内容贴进人设/SOUL 或长期记忆，按文档第七节（token 省流监控）建 30 分钟定时巡检任务；
- **Kimi Code / IDE agent**：文件放 taskflow 软件目录（`~/software/taskflow/AGENTS.md`），在 `~/software/taskflow` 启动即生效。

核心原则：agent 只做诊断、建议和经授权的操作；一切超算变更必须经过 `tf`，禁止 agent 直接拼 ssh/sbatch/scancel/rm。

---

## 10. 目录结构与版本管理

推荐布局：所有 taskflow 文件收进一个独立目录，版本收进 `versions/`，技能脚本收进 `skill/`：

```
~/software/taskflow/            # 软件包（程序 + 默认模板 + 技能 + 文档，全部在这里）
├── versions/
│   ├── <旧版本>/tf             # 旧版本留档
│   └── v1.0/tf                 # 当前版本主程序
├── setting/                    # 站点配置中心
│   ├── tf.yaml                 #   全局配置（host + project_roots + 每技能 work_dir/max_jobs）
│   ├── tf_default.yaml         #   项目配置模板 → project_setting/tf_<项目名>.yaml
│   ├── <hpc>.yaml              #   每台超算：jzzn.yaml / a800.yaml / 3090.yaml（name/ssh_host/环境/work_dir）
│   ├── <hpc>/templates/        #   该超算的提交模板（逻辑名即文件名，含 <步骤名>/ 变体）
│   └── .migrated/              #   旧提交模板备份（稳定后可删）
├── skill/                      # 任务技能脚本
│   ├── band-dft-cpu/  elastic-dft-cpu/  ke-dft-cpu/  kl-dft-cpu/  opt-dft-cpu/   # VASP 类
│   ├── kl-mace-cpu/  kl-mace-gpu/  opt-mace-cpu/  opt-mace-gpu/  phonon-mace-cpu/  # MACE 类
│   ├── mlff-mace/              #   MLFF 训练（随机位移法产出 MACE 势）
│   └── _common/                #   公共池（relax_common/dim_common/stepconf/… + mace/ + opt/）
├── scripts/mace_mu/            # 本地算参考化学势 μ 的固化脚本（形成能用）
├── TASKFLOW.md                 # 本文件（用户手册 + 技能总览 + 开发规范）
└── AGENTS.md                   # 智能体操作规范（只放这里，项目文件夹不放）
~/.local/bin/tf -> ~/software/taskflow/versions/v1.0/tf   # 软链接 = 当前生效版本
```

项目侧（project_roots 登记，如 `/mnt/d/tf_data`）：

```
<mnt/d/tf_data>/
└── C20/
    ├── qHPC20/
    │   ├── POSCAR          # 输入文件在本地
    │   ├── project_setting/    # 材料专用配置（tf init 生成，每个材料一份；可就近向上共享）
    │   │   ├── tf_qHPC20.yaml  #   材料配置（命名全局唯一；缺省继承全局/上级/skill.yaml）
    │   │   ├── setting.yaml    #   路径/结果/日志/fetch 清单
    │   │   ├── hpc.yaml        #   超算 + 模板映射（换超算改它）
    │   │   └── templates/      #   项目级模板/step.conf 覆盖（优先级最高）
    │   ├── band-dft-cpu/       # 技能子目录（skill_subdir：result/ + log/ + 技能私有 hpc.yaml）
    │   └── elastic-dft-cpu/    # 同上结构（多技能互不干扰）
    └── qTPC20-b/           # 同上结构
超算（jzzn）：只放 work_dir 计算目录树（tf 自动建），无需存放任何脚本
```

```bash
# 初次安装
mkdir -p ~/software/taskflow/versions/v1.0 ~/software/taskflow/setting ~/.local/bin
cp tf ~/software/taskflow/versions/v1.0/tf && chmod +x ~/software/taskflow/versions/v1.0/tf
cp tf.example.yaml ~/software/taskflow/setting/tf.yaml      # 按项目编辑
ln -sf ~/software/taskflow/versions/v1.0/tf ~/.local/bin/tf
tf --version                               # 查看当前版本

# 以后升级（拿到新版 tf）
mkdir -p ~/software/taskflow/versions/v1.1
cp 新tf ~/software/taskflow/versions/v1.1/tf && chmod +x ~/software/taskflow/versions/v1.1/tf
ln -sf ~/software/taskflow/versions/v1.1/tf ~/.local/bin/tf   # 切换

# 出问题一键回滚
ln -sf ~/software/taskflow/versions/<旧版本>/tf ~/.local/bin/tf
```

要点：

- **配置只有一份**（`setting/tf.yaml`，`~/software/taskflow/tf.yaml` 也可被自动搜索到；当前目录 ./tf.yaml 优先级最高），`versions/vX.Y/` 和旧版 `vX.Y/` 平铺布局都能自动找到，升级不用动配置。
- **skill_dir**：gen 需要的脚本/模板在材料目录缺失时，tf 从本地 skill 目录**经 ssh 推送**到超算（base64 编码，只补不覆盖），超算上无需再集中存放。`gen_dir` 保留为远端兜底。
- **setting/<hpc>.yaml**：包内默认超算配置；项目 `project_setting/hpc.yaml` 缺省时回退到它。新增一台超算 = 加一份 `<名>.yaml` + `<名>/templates/`（见 5.4）。
- 版本目录名随意（`v1.0`、`2026-07` 都行），软链接指谁谁是当前版。

---

## 11. 文档清单

| 文件 | 定位 | 维护约定 |
|---|---|---|
| `TASKFLOW.md`（本文件） | 总文档：用户手册 + 技能总览 + 技能开发规范 | 改功能/技能时同步 |
| `AGENTS.md` | LLM/agent 操作规范（角色、铁律、监控、汇报） | 与本文第 9 节呼应 |
| `skill/<技能>/README.md` | 各技能细节（流水线、参数、局限） | 技能改代码时同步 |
| `scripts/mace_mu/README.md` | 参考化学势计算脚本 | 换 MACE 模型后 μ 需重算 |
