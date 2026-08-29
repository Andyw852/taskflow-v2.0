# CONTEXT.md —— taskflow v2.0 代码导航（给 AI / 维护者）

> 本文件是 taskflow-v2.0 的代码地图，读代码 / 改代码前先看这里。
> 一句话：v2.0 把原 7463 行单体脚本 `versions/v1.0/tf` 拆成 `tfpkg/` 包，
> 用「单一命名空间装配」保证行为与原版完全等价。

## 1. 目录结构

```
taskflow-v2.0/
├── bin/tf                  # ★ 入口脚本（等价于原 versions/v1.0/tf）
├── tfpkg/                  # 新拆分的 Python 包
│   ├── __init__.py         # 装配器：按顺序 exec _slice/*.py 到单一命名空间
│   ├── __main__.py         # python -m tfpkg
│   ├── _collector_remote.py  # 远端采集脚本（真实 .py，可 lint；装配时读成 COLLECTOR 字符串）
│   └── _slice/             # 18 个分片（按职责切，见第 4 节地图）
├── versions/v1.0/tf        # 原始单体（保留作对比基准，勿改）
├── skill/                  # 技能（模板 / 脚本 / skill.yaml）
├── setting/                # 集群 / 全局配置（tf.yaml、<集群>.yaml）
├── scripts/                # 辅助脚本（sanitize、git push）
├── test/                   # 测试夹具
├── AGENTS.md               # agent 操作规范（铁律 / 命令 / 判读 / 决策）
└── TASKFLOW.md             # 完整手册
```

## 2. 架构：单一命名空间装配（single-namespace assembly）

原 `tf` 是 7463 行的单体脚本，184 个函数 / 类 / 常量共享一个模块级命名空间，
函数之间按名字直接引用，且存在多处**循环依赖**（见第 5 节）。

v2.0 的做法：

- 把原文件按职责切成 `tfpkg/_slice/` 下的 18 个分片（函数体**逐字拷贝，零改动**）。
- `__init__.py` 在**同一个** `globals()` 命名空间里按文件名顺序 `exec` 这些分片。
- 效果 = 原单文件：所有函数 / 常量互相按名字可见，无需任何 import 改写。

**改代码时的规则**：

- 改某个功能 → 打开对应分片（见第 4 节地图）。
- 分片之间的引用按名字解析，**不要给分片加 import**（它们不是独立模块）。
- 装配顺序 = 依赖顺序：`00_state.py` 先注入标准库 import 和全部常量。

## 3. 关键不变量（改代码别破坏）

1. **路径常量**（原 `__file__` 深度 hack 已消除）：`__init__.py` 注入 `_PKG_ROOT`（包根）、
   `_PKG_DIR`（tfpkg 包目录）、`_SLICE_DIR`（_slice 目录）三个显式常量，替代分片里
   `dirname(__file__)/../..` 之类的路径计算；`__file__` 保持真实值 `tfpkg/__init__.py`。
   **不要移动 `_slice/` 目录的层级**（`_SLICE_DIR` 仍指向它）。
2. **`COLLECTOR` 独立成 `_collector_remote.py`**：远端采集脚本现在是一个真实、可 lint
   的 `.py` 文件 `tfpkg/_collector_remote.py`；`__init__.py` 装配时把它读回成字符串注入
   命名空间，运行时字节与原单体完全一致（base64 下发超算的行为不变）。
3. **命令入口**：`main()` 在 `17_cli.py`，命令分发逻辑也在其中。
4. **副作用顺序**：装配时 `00_state.py` 里会执行 `os.environ.get(...)` 等常量求值，
   其余分片只有 `def` / `class` 定义，不产生副作用。

## 4. 分片地图（改哪找哪）

| 分片 | 职责 | 主要函数 |
|---|---|---|
| 00_state | 常量 / 配置模板 / 标准库 import / JSON_SCHEMA | EXAMPLE_CONFIG, USAGE, STATUS_ALIAS, JSON_SCHEMA, 各缓存 dict |
| 01_yamlmini | 迷你 YAML 解析器 + 配置加载 | _mini_yaml, load_config |
| 02_skills | 技能发现 / 清单 / 装配 | discover_skills, apply_skills, cmd_skills |
| 03_projects | 项目配置扫描合并 / 任务类型 | merge_project_configs, get_types, step_cfg |
| 04_discover | 本地材料 / 目录发现 | discover_local, resolve_material_local |
| 05_collect | 远端采集（ssh + COLLECTOR） | collect, collect_v3_batch, run_remote |
| 06_state | 步骤状态机 / DAG / 门控 | step_state, _dag_recompute, _SkillGate, annotate |
| 07_render | 表格 / 详情渲染 + 查找 | render_table, render_detail, find_material |
| 08_assets | 技能资源 / step.conf | find_asset, build_step_conf, fill_local_dim |
| 09_submit | 远端生成 / 提交 / 取消 | remote_gen, remote_sbatch, do_submit, do_rerun_step |
| 10_summary | status / summary | cmd_status, cmd_summary, _summary_json |
| 11_actions | start / stop / retry / rerun | cmd_start, cmd_stop, cmd_retry, cmd_rerun |
| 12_hang | 挂死检测 / 恢复 | auto_recover_hung, _hung_scan |
| 13_advance | 自动推进 / 拉回 / clean | auto_advance, auto_fetch, cmd_clean |
| 14_init | 项目初始化 | cmd_init, _init_one_skill |
| 15_hpc | hpc / level / auto / adopt / migrate | cmd_hpc, cmd_level, cmd_auto, cmd_adopt |
| 16_watch | 后台监控 | cmd_watch, _watch_daemon |
| 17_cli | 状态缓存 / 过滤 / main 入口 | main, collect_data, filter_status |

## 5. 已知循环依赖（将来做「深模块」时先读这节）

拆分前做了全量依赖分析，发现以下循环。若将来把分片改成真实 import 的独立模块，
必须**先打破这些环**（用延迟 import / 接口抽取），否则会 import 失败：

- `06_state ↔ 09_submit ↔ 13_advance ↔ 11_actions`（工作流执行主环）
- `15_hpc ↔ 17_cli ↔ 16_watch`（命令路由环）

**环的具体边（函数级，2025 实测）**：

- 环1（工作流执行，真正纠缠，破环需延迟 import）：
  - `06_state → 09_submit`：`remote_gen`
  - `09_submit → 06_state`：`_scancel_clear`
  - `09_submit → 13_advance`：`_fetch_stamp_clear`、`_relay_prev_across_host`、`fetch_material`
  - `13_advance → 09_submit`：`_fanout_guard`、`do_submit`、`kill_if_queued`、`remote_gen`、`remote_scancel`
  - `13_advance → 11_actions`：`guard_predecessors`、`step_targets`
  - `11_actions → 06_state`：`_SkillGate`、`_dag_max_inflight`、`_dag_recompute`、`_gen_step_input`、`_pregenerate_ready`、`_scancel_clear`、`_scancel_set`
  - `11_actions → 09_submit`：`_scancel_desc`、`do_rerun_step`、`do_submit`、`kill_if_queued`、`remote_scancel`、`tag_of`
- 环2（命令路由，有清晰缝）：
  - `15_hpc → 17_cli`：`collect_data`
  - `17_cli → 15_hpc`：`cmd_adopt`、`cmd_auto`、`cmd_auto_project`、`cmd_auto_skill`、`cmd_hpc`、`cmd_level`、`cmd_migrate_subdir`
  - `16_watch → 17_cli`：`_snapshot`、`_state_cache_save`、`apply_exclude`、`collect_data`、`filter_projs`
  - `17_cli → 16_watch`：`_watch_cron`、`_watch_daemon`、`_watch_ensure`、`_watch_stop`、`cmd_watch`
  - 破环缝：把 `collect_data`/`_snapshot`/`_state_cache_save`/`apply_exclude`/`filter_projs`
    （数据采集/过滤工具，17 定义、被 15/16 依赖）抽成叶子深模块 `tfpkg/data.py`，
    15/16 依赖它而非 17，环2 即破。

这正是 v2.0 选用「单一命名空间装配」而非真实 import 的原因——它是唯一能
在不改动任何函数体、保证行为等价的前提下完成拆分的方案。

## 6. 如何运行 / 验证

```bash
cd ~/software/taskflow-v2.0
python3 bin/tf --help        # 帮助（与原版逐字节一致）
python3 bin/tf skills        # 只读，读 skill/*/skill.yaml
python3 bin/tf config        # 打印示例配置
python3 -m tfpkg             # 等价入口
python3 -c "import tfpkg"    # 装配成功 = 184 个定义就位
```

只读回归命令（不碰超算）：`--help` / `config` / `skills` / `--schema`。

单测：`python3 test/test_tfpkg.py`（14 个纯函数 + CLI 冒烟用例，自带运行器，无需 pytest）。

## 7. 已知差异（已消除）

- ~~`tf --version` / 裸 `tf` 的「程序:」行显示 `_slice/00_state.py`~~ —— 已修复：
  `__init__.py` 注入 `_PROG_PATH` 指向真实入口 `bin/tf`，版本 / 裸 tf 的「程序:」行
  改用 `_PROG_PATH` 显示；`包根` 计算仍用 `__file__`（保持深度不变量）。
  现在 `--help` 与原版逐字节一致，`--version` 显示 `bin/tf`。

## 8. v2.0 增强（已完成）

- **COLLECTOR 独立**：`tfpkg/_collector_remote.py`（真实 .py，可 `py_compile` / lint），
  装配时读回字符串注入命名空间，字节与原单体一致（有单测 `test_collector_integrity`）。
- **pytest 单测**：`test/test_tfpkg.py` 14 个用例（COLLECTOR 完整性、config 加载、
  skills 发现/装配、summary 格式化、状态词、快照 diff、自然排序、CLI 冒烟）。
  自带独立运行器，不依赖 pytest 是否安装。
- **`--dry-run`**：`start/stop/retry/rerun/clean/fetch` 加 `--dry-run`，只打印将影响的
  材料/步骤，不执行任何变更、不提交作业。
- **`tf json` schema**：输出加 `schema_version: 2` + `tf_version`；`tf json --schema`
  （或 `tf --schema`）打印字段说明（常量 `JSON_SCHEMA`）。
- **`--json` 输出**：`list/summary/status/dir` 加 `--json`，输出机器可读 JSON
  （summary 用 `_summary_json` 结构化；status 的 `--json` 只读、不 auto_fetch/advance）。

## 9. v2.0 与原版 skill/setting 差异审计（发现并修复 1 处回归，其余待核对）

- **已修复（回归）**：`skill/_common/opt/stepconf.py` 的 `RESERVED_PARAMS` 缺
  `POTCAR_DIR` / `REFERENCES_DIR`（原版有，v2.0 拷贝时漏了）。后果：`setting/jzzn.yaml`
  把这两个 VASP 键注入所有技能 step.conf，MACE 生成脚本白名单不认识 → `retry` gen 报
  「不认识的键」、整批 30 个材料 retry 失败。已补回，与原版逐字节一致。
- **其余待核对差异**（diff -rq 得出，均不影响 opt-mace-cpu）：
  `skill/ke-dft-cpu/step3_uniform/gen_step5_uniform.py`、
  `skill/ke-dft-cpu/step7_deform/step7b_read/gen_step9b_deform_read.py`、
  `skill/mlff-mace/benchmark.py`、`setting/hanhai25.yaml` 及其 templates。
- **已解决**：全量 `rsync -ac --delete` 把 v2.0 的 skill/ + setting/ 与原版内容级对齐，
  上述差异全部消除；新增 `scripts/sync_check.sh` 持续审计（见 §10）。

## 10. AI 友好增强（第二波）与待办

- **git 化**：v2.0 已 `git init` 并推送 GitHub（`github.com/Andyw852/taskflow-v2.0`）。
  `.gitignore` 排除 `setting/`（配置）、`*.model`（大模型权重）、`test/` 数据子目录，
  只跟踪代码（294 文件 ~4.7MB）。
- **对齐自动审计**：`scripts/sync_check.sh` 一键 diff v2.0 vs 原版 skill/setting
  （内容级，排除 __pycache__/运行时缓存），退出码 0=对齐 / 1=有差异并给同步命令。
- **dry-run 精确化**：`--dry-run` 按命令语义打印真实目标——`retry`=FAIL 步、
  `start`=就绪步、`stop`=有作业步、`fetch`=已完成步；`rerun/clean` 无 `-j` 时整材料级
  （`_dry_run_steps_for` / `_dry_run_report`，见 17_cli.py）。
- **结构化错误码**：`_diag_code(diag)`（10_summary.py）把诊断文本归到稳定 code，
  `--json` 输出的 step 加 `diag_code`、summary 的 `fails[].code` 也带 code，
  agent 无需 grep（如 `relax_summary_missing` / `relax_summary_incomplete` /
  `force_not_converged` / `relax_oscillating` / `stepconf_unknown_params`）。
- **测试**：`test/test_tfpkg.py` 14 → 19 个用例（新增 stepconf 白名单回归、
  retry 目标、dry-run 语义、diag_code、yamlmini 深模块）。

**深模块化（进行中）**：
- 已完成：`__file__` 深度 hack 消除（改 `_PKG_ROOT`/`_PKG_DIR`/`_SLICE_DIR` 常量）；
  YAML 解析器抽成真深模块 `tfpkg/yamlmini.py`（对外接口 `parse()`，4 个内部函数
  隐藏 ~200 行实现，由 `__init__.py` 导入后注入共享命名空间）。
- 待办：两个环的破环（函数级边图见 §5）——环2 有清晰缝（抽 `tfpkg/data.py`），
  环1 需延迟 import；均影响在跑的 tf，高风险，建议逐环、逐函数测试推进。

