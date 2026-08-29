# CONTEXT.md —— taskflow v2.0 代码导航（给 AI / 维护者）

> 本文件是 taskflow-v2.0 的代码地图，读代码 / 改代码前先看这里。
> 一句话：v2.0 把原 7463 行单体脚本 `versions/v1.0/tf` 拆成 `tfpkg/` 包的
> 8 个真深模块（小接口 + 深实现），行为与原版完全等价。

## 1. 目录结构

```
taskflow-v2.0/
├── bin/tf                  # ★ 入口脚本（等价于原 versions/v1.0/tf）
├── tfpkg/                  # 新拆分的 Python 包
│   ├── __init__.py         # 装配器：导入 8 个真深模块并把名字注入包命名空间
│   ├── __main__.py         # python -m tfpkg
│   ├── _collector_remote.py  # 远端采集脚本（真实 .py，可 lint；装配时读成 COLLECTOR 字符串）
│   ├── bootstrap.py        # 配置/发现流
│   ├── collect.py          # 远端采集
│   ├── data.py             # 数据簇（采集+过滤+缓存+快照）
│   ├── workflow.py         # 工作流执行引擎
│   ├── report.py           # 渲染/汇报流
│   ├── ops.py              # 运维流
│   ├── cli.py              # 命令入口
│   └── yamlmini.py         # YAML 解析器
├── versions/v1.0/tf        # 原始单体（保留作对比基准，勿改）
├── skill/                  # 技能（模板 / 脚本 / skill.yaml）
├── setting/                # 集群 / 全局配置（tf.yaml、<集群>.yaml）
├── scripts/                # 辅助脚本（sanitize、git push）
├── test/                   # 测试夹具
├── AGENTS.md               # agent 操作规范（铁律 / 命令 / 判读 / 决策）
└── TASKFLOW.md             # 完整手册
```

## 2. 架构：深模块（deep modules，小接口 + 深实现）

原 `tf` 是 7463 行的单体脚本，184 个函数 / 类 / 常量共享一个模块级命名空间，
函数之间按名字直接引用，且存在多处**循环依赖**（见第 5 节，均已消除）。

v2.0 的做法：

- 把原文件按职责合并成 `tfpkg/` 下的 8 个真深模块（见第 4 节地图）。
- `__init__.py` 导入这 8 个模块，并把它们的名字注入包命名空间。
- 跨模块引用在**函数内**用 `from tfpkg import X` 延迟解析（调用时才解析，避开模块级 import 环）。

**改代码时的规则**：

- 改某个功能 → 打开对应模块（见第 4 节地图）。
- 跨模块引用：函数内 `from tfpkg import X`（不要加模块级 import，避免环）。
- 模块内引用：直接按名字调用（同模块）。

## 3. 关键不变量（改代码别破坏）

1. **路径常量**（原 `__file__` 深度 hack 已消除）：`__init__.py` 注入 `_PKG_ROOT`（包根）、
   `_PKG_DIR`（tfpkg 包目录）、`_SLICE_DIR`（_slice 目录）三个显式常量，替代分片里
   `dirname(__file__)/../..` 之类的路径计算；`__file__` 保持真实值 `tfpkg/__init__.py`。
   （`_SLICE_DIR` 已废弃——`_slice/` 目录已删除，所有代码并入真模块。）
2. **`COLLECTOR` 独立成 `_collector_remote.py`**：远端采集脚本现在是一个真实、可 lint
   的 `.py` 文件 `tfpkg/_collector_remote.py`；`__init__.py` 装配时把它读回成字符串注入
   命名空间，运行时字节与原单体完全一致（base64 下发超算的行为不变）。
3. **命令入口**：`main()` 在 `cli.py`，命令分发逻辑也在其中。
4. **副作用顺序**：装配时 `00_state.py` 里会执行 `os.environ.get(...)` 等常量求值，
   其余分片只有 `def` / `class` 定义，不产生副作用。

## 4. 模块地图（改哪找哪）

| 模块 | 职责 | 主要函数 |
|---|---|---|
| bootstrap.py | 配置/发现流（常量/配置模板/标准库 import/YAML 加载/技能发现/项目配置/材料发现） | load_config, discover_skills, apply_skills, merge_project_configs, discover_local, EXAMPLE_CONFIG, STATUS_ALIAS |
| collect.py | 远端采集（ssh + COLLECTOR） | collect, collect_v3_batch, run_remote, sh_b64 |
| data.py | 数据簇（采集 + 过滤 + 缓存 + 快照） | collect_data, apply_exclude, filter_projs, filter_status, _snapshot, _state_cache_save |
| workflow.py | 工作流执行引擎（状态机/DAG/门控 + 提交 + 推进 + 动作命令） | step_state, do_submit, auto_advance, auto_fetch, remote_gen, cmd_start/stop/retry/rerun/clean, annotate |
| report.py | 渲染/汇报流（表格/详情渲染 + 查找 + 资源 + status/summary） | render_table, render_detail, find_material, find_asset, cmd_status, cmd_summary |
| ops.py | 运维流（挂死检测/恢复 + init + hpc 切换 + watch） | auto_recover_hung, cmd_init, cmd_hpc, cmd_level, cmd_watch |
| cli.py | 命令入口（main + 分发 + dry-run） | main |
| yamlmini.py | YAML 解析器 | parse |

## 5. 历史循环依赖（均已消除，供参考）

拆分前做了全量依赖分析，发现以下循环。深模块化时已用「合并成真模块 + 函数内延迟
import」全部打破（见 §10），此处保留原始边图供追溯：

- `06_state ↔ 09_submit ↔ 13_advance ↔ 11_actions`（工作流执行主环）✅ **已破**：四片合并为真模块 `tfpkg/workflow.py`
- `15_hpc ↔ 17_cli ↔ 16_watch`（命令路由环）✅ **已破**：数据簇抽 `tfpkg/data.py`

**环的具体边（函数级，2025 实测）**：

- 环1（工作流执行，真正纠缠，破环需延迟 import）：
  - `06_state → 09_submit`：`remote_gen`
  - `09_submit → 06_state`：`_scancel_clear`
  - `09_submit → 13_advance`：`_fetch_stamp_clear`、`_relay_prev_across_host`、`fetch_material`
  - `13_advance → 09_submit`：`_fanout_guard`、`do_submit`、`kill_if_queued`、`remote_gen`、`remote_scancel`
  - `13_advance → 11_actions`：`guard_predecessors`、`step_targets`
  - `11_actions → 06_state`：`_SkillGate`、`_dag_max_inflight`、`_dag_recompute`、`_gen_step_input`、`_pregenerate_ready`、`_scancel_clear`、`_scancel_set`
  - `11_actions → 09_submit`：`_scancel_desc`、`do_rerun_step`、`do_submit`、`kill_if_queued`、`remote_scancel`、`tag_of`
  - **✅ 已破**：四片合并成 `tfpkg/workflow.py`（上述内部边全变模块内引用，无 import），
    外部依赖（00/02/03/04/05/07/08/14）在函数内 `from tfpkg import ...` 延迟解析。
- 环2（命令路由，有清晰缝）：
  - `15_hpc → 17_cli`：`collect_data`
  - `17_cli → 15_hpc`：`cmd_adopt`、`cmd_auto`、`cmd_auto_project`、`cmd_auto_skill`、`cmd_hpc`、`cmd_level`、`cmd_migrate_subdir`
  - `16_watch → 17_cli`：`_snapshot`、`_state_cache_save`、`apply_exclude`、`collect_data`、`filter_projs`
  - `17_cli → 16_watch`：`_watch_cron`、`_watch_daemon`、`_watch_ensure`、`_watch_stop`、`cmd_watch`
  - 破环缝：把 `collect_data`/`_snapshot`/`_state_cache_save`/`apply_exclude`/`filter_projs`
    （数据采集/过滤工具，17 定义、被 15/16 依赖）抽成叶子深模块 `tfpkg/data.py`，
    15/16 依赖它而非 17，环2 即破。
  - **✅ 已破**：`tfpkg/data.py` 已抽出（见 §10），15/16 不再依赖 17 的数据函数。

这正是 v2.0 选用「单一命名空间装配」而非真实 import 的原因——它是唯一能
在不改动任何函数体、保证行为等价的前提下完成拆分的方案。

## 6. 如何运行 / 验证

```bash
cd ~/software/taskflow-v2.0
python3 bin/tf --help        # 帮助（与原版逐字节一致）
python3 bin/tf skills        # 只读，读 skill/*/skill.yaml
python3 bin/tf config        # 打印示例配置
python3 -m tfpkg             # 等价入口
python3 -c "import tfpkg"    # 装配成功 = 8 个深模块就位
```

只读回归命令（不碰超算）：`--help` / `config` / `skills` / `--schema`。

单测：`python3 test/test_tfpkg.py`（22 个用例，自带运行器，无需 pytest）。

## 7. 已知差异（已消除）

- ~~`tf --version` / 裸 `tf` 的「程序:」行显示 `_slice/00_state.py`~~ —— 已修复：
  `__init__.py` 注入 `_PROG_PATH` 指向真实入口 `bin/tf`，版本 / 裸 tf 的「程序:」行
  改用 `_PROG_PATH` 显示；`包根` 计算仍用 `__file__`（保持深度不变量）。
  现在 `--help` 与原版逐字节一致，`--version` 显示 `bin/tf`。

## 8. v2.0 增强（已完成）

- **COLLECTOR 独立**：`tfpkg/_collector_remote.py`（真实 .py，可 `py_compile` / lint），
  装配时读回字符串注入命名空间，字节与原单体一致（有单测 `test_collector_integrity`）。
- **pytest 单测**：`test/test_tfpkg.py` 22 个用例（COLLECTOR 完整性、config 加载、
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
- **测试**：`test/test_tfpkg.py` 14 → 22 个用例（新增 stepconf 白名单回归、
  retry 目标、dry-run 语义、diag_code、yamlmini/data/workflow 深模块、命名空间完整性）。

**深模块化（已完成）**：
- 全部 8 个真深模块就位：`bootstrap.py`（配置/发现流）、`collect.py`（远端采集）、
  `data.py`（数据簇）、`workflow.py`（工作流执行引擎）、`report.py`（渲染/汇报流）、
  `ops.py`（运维流）、`cli.py`（命令入口）、`yamlmini.py`（YAML 解析器）。
  `_slice/` 目录已删除，单一命名空间装配已退役。
- 两个循环依赖环均已消除（环1 合并进 workflow.py、环2 抽 data.py）；跨模块引用
  统一用函数内 `from tfpkg import ...` 延迟解析。
- 22 个单测（含命名空间完整性安全网 `test_namespace_complete`）。

