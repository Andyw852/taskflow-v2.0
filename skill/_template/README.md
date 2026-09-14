# skill/_template/ —— 新技能模板（复制我，30 分钟上线）

> v1.0 配套。目标：**加一个技能 = 复制一个目录 + 改 5 处**，不用改 tf 核心、
> 不用改 tf.yaml、不用问作者。

## 0. 先跑一遍看效果（3 分钟）

```bash
cd ~/software/taskflow-v1.0                  # 软件仓库根
cp -r skill/_template skill/my-skill         # ① 复制目录
sed -i 's/^enabled: false.*$//; s/^name: template.*$/name: my-skill/' skill/my-skill/skill.yaml
# （skill.yaml 里带行内注释，所以用 .*$ 兜掉注释再替换）
python3 bin/tf skills                        # ② 零配置自动发现：my-skill 出现在表里
python3 bin/tf schema my-skill               # ③ 看它的自描述（吃什么/吐什么/能接谁/怎么纠错）
```

第 ② 步是关键：**tf 只认 `skill/<名字>/skill.yaml`，放进目录即被自动发现**，
不需要在 tf.yaml 里登记任何东西。

## 1. 要改的 5 处

| # | 改哪 | 说明 |
|---|---|---|
| 1 | `skill.yaml` 的 `enabled: false` | 删掉（或改 true）——模板默认不启用，免得污染技能表 |
| 2 | `skill.yaml` 的 `name` / `desc` / `version` | `name` = 全局唯一技能 key = 以后 `tf -tt <name>` 用的名字 |
| 3 | `skill.yaml` 的 `steps` | 你的流水线：每步一个 gen 脚本 + 一个判据；`seq` 决定顺序 |
| 4 | `gen_step1_summary.py` | 换成你自己的生成/后处理脚本（见第 3 节） |
| 5 | `io_schema` / `flow` / `corrections` 三段 | 自描述；`tf schema <技能>` 会校验，写错会给出 [警告]/[错误] |

## 2. skill.yaml 关键字段速查

| 字段 | 作用 |
|---|---|
| `schema: 2` | 允许带 `io_schema`/`flow`/`corrections` 自描述段（老技能写 1 也照跑） |
| `steps[].name` | 步骤目录名（远端 `材料/<技能>/<name>/`） |
| `steps[].label` | 显示短名，`tf -j S1_summary` 用它 |
| `steps[].gen` | gen 脚本（可带参数：`"gen_x.py --stage a"`） |
| `steps[].check` | 完成判据：`outcar` / `wavecar` / `relax_injob`（公共池）/ `plot` |
| `steps[].run: gen` | 该步**不交 SLURM**，在登录节点跑脚本（画图/汇总这类） |
| `steps[].done_marker` | run: gen 步的产出文件名（tf 据此判完成并自动拉回） |
| `steps[].gen_need` | 随 gen 一起推到材料目录的依赖（模板/公共库/step.conf） |
| `steps[].needs` | 显式声明依赖的步骤（不写 = 依赖上一步） |
| `optional_steps` | 可选步骤组（一个开关名管一组步，如画图/HSE 段） |
| `fetch_files` | 算完自动拉回本地 result/ 的文件清单 |
| `defaults.hpc` / `work_dir` | 站点相关缺省值（用户在 tf.yaml / 项目配置里覆盖） |

## 3. gen 脚本的契约（★ 最容易踩的地方）

- tf 把脚本推到远端，**cwd = 该步骤目录**（`材料/<技能>/<步骤名>/`）；
- 脚本自己写 `INCAR/KPOINTS/POSCAR/submit.sh`（要提交 SLURM 的步）；
- 需要模板/公共库时写进 `gen_need`，tf 会一起推过去；
- `gen` 里可以写 `{mat}` / `{matdir}` / `{root}` / `{step}` / `{tt}` 占位符，tf 会替换；
- 参数从 `step.conf` 读（公共池 `stepconf.py`，见 opt-dft-cpu 的用法），
  用户改参数走 `tf -p X -j <步骤> conf --set params.KEY=值`。

**照抄对象**（别从零写）：
| 你要做的 | 抄这个 |
|---|---|
| 提交 VASP 的静态自洽步 | `skill/opt-dft-cpu/gen_step2_static.py` |
| 结构弛豫步 | `skill/opt-dft-cpu/gen_step1_opt.py`（薄壳，逻辑在公共池 `_common/opt/relax_common.py`） |
| INCAR 模板 | `skill/opt-dft-cpu/templates/incar_{2d,3d}.tpl` |
| 提交脚本模板 | `setting/<集群>/templates/submit_*.tpl`（站点相关，**不随技能走**） |
| 画图/后处理步 | `skill/band-dft-cpu/gen_step3.1_plot_band.py`、`skill/opt-dft-cpu/gen_step3_energy.py` |
| 判据（自定义完成判据） | `skill/_common/checks_relax.py`、`skill/*/checks.py` |
| 可选步骤组 | `skill/band-dft-cpu/skill.yaml` 的 `optional_steps` |
| 扇出步骤（一堆子目录） | `skill/mlff-mace/`、`skill/defect-dft-cpu/` |

## 4. 自描述三段（v1.0 新增，为什么值得写）

- `io_schema` —— 这个技能**吃什么、吐什么、有哪些旋钮**。写了以后：
  新人（和 AI）不用读代码就知道怎么用、产物在哪；参数写错会在 `tf schema` 里报出来。
- `flow` —— **整条流程在干什么、产物能喂给谁**（`next_skills`）。这是"技能之间能串起来"
  的基础，也是对标 atomate2 Maker/flow 的那一层。
- `corrections` —— **这类失败该怎么纠**，引用 `skill/_common/_corrections/` 里的
  handler（见那份 README）。写了以后 `tf diagnose` / `tf correct` 会给具体建议。

校验：`python3 bin/tf schema <技能>`（`--strict` 有错误时返回非零，可进 CI）。

## 5. 上线前自检清单

```bash
python3 bin/tf skills                 # ① 被发现了？（版本/步骤数/自描述列）
python3 bin/tf schema <技能> --strict  # ② 自描述没写错？
python3 bin/tf -tt <技能> init         # ③ 能在项目里初始化配置段？
python3 bin/tf -tt <技能> -p <材料> -j <步骤> init   # ④ 输入生成对不对（只生成不提交）
python3 bin/tf -tt <技能> -p <材料> -j <步骤> start  # ⑤ 确认无误再提交
```

★ 第 ③~⑤ 步会真的碰项目/超算：先确认你是在**自己的测试项目**里做，别在跑着的正产项目上试。
