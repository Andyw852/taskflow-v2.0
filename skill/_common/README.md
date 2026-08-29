# skill/_common —— 公共技能池

技能目录里找不到的依赖和模板，回落到这里。放这里的东西**只写一份**，
所有技能共用；改公共池 = 所有技能一起改。

## 为什么现在做

之前的约定是"每技能独立复制 helper"，那时成本很低。现在仓库里：

| 文件 | 副本数 | 是否字节相同 |
|---|---|---|
| `dim_common.py` | 11 | 全部相同 |
| `check_common.py` | 11 | 全部相同 |
| `stepconf.py` | 5 | 全部相同 |
| `ke_common.py` | 3 | 全部相同 |

给 `dim_common` 加 0D 支持要改 11 个地方、且没有任何机制保证它们不漂移——
这就是把天平压过去的那件事。

## 目录布局

```
skill/
  _common/                  ← 公共池（没有 skill.yaml，所以不会被当成技能）
    dim_common.py           维度判定 / 模板选择 / KPOINTS 修正（已含 0D）
    stepconf.py             step.conf 三层合并读取
    check_common.py         check 脚本共用工具
    mol_common.py           0D（孤立分子）分支
    relax_common.py         结构优化 step1 引擎  ← 通用技能主体
    templates/
      incar_0d.tpl          0D 的 INCAR 模板（所有技能共用）
  band-dft-cpu/  elastic-dft-cpu/  ke-dft-cpu/  kl-dft-cpu/ ← 各技能只留自己独有的东西
```

## 解析优先级（tf 补丁）

`tf_common_pool.patch` 在 `_skill_asset_dirs()` 末尾追加两条兜底路径：

```
<技能>/templates/<步骤名>/  →  <技能>/templates/  →  <技能>/
                            →  _common/templates/  →  _common/
```

含义：**技能目录里有同名文件就优先用自己的**，没有才用池子里的。
所以迁移可以一个技能一个技能来——删掉某技能的 `dim_common.py`，它就自动
改用池子里的；不删就还是用自己的。`gen_need` 清单**一个字都不用改**，
因为清单写的是文件名，变的只是 tf 到哪里去找这个文件。

`_common` 没有 `skill.yaml`，而技能发现只认 `skill.yaml`（`tf:994`），
所以池子不会被误认成技能。

## 各模块接口

### dim_common

```python
detect_dimension(poscar_path, vacuum_min=8.0, allow_0d=None)
    -> (dim, axis, vacuums)      dim = "0d" | "2d" | "3d"
                                 axis = 真空轴 0/1/2（2D），其余为 None
read_dim(method_file)     -> "0d"|"2d"|"3d"|None    读 workflow_method.txt 的 DIM=
resolve_dim(method_file, struct_path, vacuum_min=8.0) -> (dim, note)
                                 优先继承上一步的 DIM=，缺失才现场判定
resolve_tpl(base_dir, base, dim) -> Path            <base>_<dim>.tpl，回退 <base>.tpl
force_kz1(kpoints_path, axis=2)  -> (changed, note)
validate_poscar(path)            -> None | 问题描述   （接力结构完整性）
```

模块级开关 `ALLOW_0D = True`：>=2 个真空方向时返回 `"0d"`。设成 `False`
恢复旧的 SystemExit 行为——**不支持 0D 的技能不需要改代码**，因为它们
拿到 `"0d"` 后 `resolve_tpl` 找不到 `*_0d.tpl` 也会明确报错。

### stepconf

```python
CONF_NAME                      # "step.conf"
load(spec, step_name=None) -> dict
    spec = {"KEY": (默认值, "str")}   只读 spec 里声明过的键
```

### mol_common（0D 分支）

```python
is_molecule(poscar, vacuum_min, dimension_setting) -> bool
generate(cwd, G) -> None          G = 调用方的 globals()
```

`generate()` 通过 `G` 复用调用方的函数，**契约是 G 必须提供**：
`resolve_tpl`、`resolve_func`、`validate_user_config`、`read_poscar_identity`、
`build_params`、`render`、`validate_generated_incar`、`override_submit_slurm`、
`run_vaspkit_kpoints`、`run_vaspkit_potcar`、`encut_from_potcar`、
`read_species_and_counts`、`decide_lmaxmix`、`apply_lmaxmix_to_incar`、
`decide_u`、`apply_u_to_incar`、`write_method_file`，以及配置量
`RUN_VASPKIT`、`VASPKIT_EXE`、`KSCHEME`、`KSPACING`、`MANUAL_ENCUT`、
`ENCUT_FACTOR`、`FALLBACK_ENCUT`、`SUBMIT_OVERRIDE`、`MAG_ELEM_MOMENTS`、
`METHOD_FILE`。`relax_common` 全部满足，所以任何用 `relax_common` 的技能
白拿 0D 支持。

行为开关走 `step.conf` 的 `[params]`：`MOL_KPOINTS` / `MOL_ISPIN` /
`MOL_MOMENT` / `MOL_DIPOL` / `MOL_ENCUT_FLOOR` / `MOL_ALLOW_3D_TPL`。

### relax_common（结构优化引擎）

技能的 `gen_step1_*.py` 退化成十几行：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relax_common as R

R.run(
    OUTDIR_SINGLE="step1_PBE_opt",
    OUTDIR_PATTERN="step1%s_PBE_opt",
    SCRIPT_NAME="gen_step1_PBE_opt.py",
    NEXT_STEP="gen_step2_static.py",
    CELL_POLICY="primitive",
    MOL_BRANCH=True,
)
```

`run(**overrides)` 把覆盖值写进模块全局量再跑 `main()`。**键名必须在
`DEFAULTS` 里**，写错立刻报错而不是被静默忽略——这是防止"改了个没人读的
变量然后以为生效了"的主要手段。

常用键：

| 键 | 缺省 | 含义 |
|---|---|---|
| `OUTDIR_SINGLE` | `step1_PBE_opt` | single 模式输出目录 |
| `OUTDIR_PATTERN` | `step1%s_PBE_opt` | 分段模式目录名，`%s` 是段号 |
| `SCRIPT_NAME` / `NEXT_STEP` | — | 仅用于提示信息 |
| `JOBNAME_SUFFIX` | `_s1opt` | 作业名后缀 |
| `CELL_POLICY` | `primitive` | `primitive`（能带/声子）/ `standard`（弹性，IEEE 取向）/ `none`（缺陷、分子） |
| `STD_CELL` | `primitive_standard` | `standard` 时用原胞标准型还是 `conventional` |
| `MOL_BRANCH` | `False` | 是否启用 0D 分支 |
| `RELAX_STAGES` | `auto` | `auto` 三段式 / `single` 单段 |
| `STAGE_SPEC` / `STAGE_ORDER` | 三段 | 每段覆盖的 INCAR 标签，值为 `None` 表示删除该标签 |
| `AUTO_MAG` / `AUTO_U` / `MANUAL_ENCUT` / `ENCUT_FACTOR` / `DIMENSION` / `CELL_CONSTRAINT_2D` … | 见 `DEFAULTS` | 与原脚本同名同义 |

输入（运行目录 = 材料目录）：`POSCAR`、`incar_{0d,2d,3d}.tpl`、
`submit_std_{0d,2d,3d}.tpl`、可选 `step.conf`。
输出：`<OUTDIR>/{POSCAR,INCAR,KPOINTS,POTCAR,submit.sh,workflow_method.txt}`，
其中 `workflow_method.txt` 的 `FUNC/GGA/IVDW/DIM/MAG` 供 step2+ 继承。
失败一律 `sys.exit("[ERROR] ...")`，tf 只显示最后一行。

## 等价性验证

`relax_common` 是从 `skill/band-dft-cpu/gen_step1_PBE_opt.py` 抽出来的，抽的过程
只做了三类改动：目录名/脚本名参数化、取胞策略派发、0D 分支惰性导入。
验证方法（建议每次改公共池都重做一遍）：

```bash
# A：原脚本   B：薄壳 + relax_common，同一个 POSCAR、同一套模板
diff -r A/step1a_PBE_opt B/step1a_PBE_opt
```

在一个 2D 双笼结构上跑过：`INCAR / KPOINTS / submit.sh /
workflow_method.txt / POSCAR` 全部逐字节相同，日志只差绝对路径。
0D 分支（Li@C60，20 Å 盒）也跑通：`ISPIN=2`（243 个价电子为奇数）、
`DIPOL` 为几何中心、`ENCUT` 按 POTCAR 自动。

## 分段弛豫：统一到"作业内分段"（STAGE_MODE）

原来两套并存：band-dft-cpu 是 tf 层面的 a/b/c 三个步骤，elastic-dft-cpu/ke-dft-cpu 是一个作业里
`INCAR.s1/s2/s3` + `run_relax.sh` 顺序跑。现在统一由 `STAGE_MODE` 选择，
缺省 `in_job`：

| | `in_job`（缺省） | `tf_stages`（旧 band-dft-cpu） |
|---|---|---|
| 排队 | 1 次 | 3 次 |
| 段间接力 | 作业里 `cp CONTCAR POSCAR`，无残缺风险 | 跨步骤读上一段 CONTCAR，要 `validate_poscar` 防读到写了一半的文件 |
| 提前收敛 | 某段收敛就跳过后续段（`EARLY_EXIT`） | `ck_relax_skip` 在 tf 侧跳过 |
| 断点续跑 | `.sN.done` 标记，重投自动跳过已完成段 | 重投整段 |
| 段间换资源 | 不行（同一个作业） | 可以 |
| 单独重跑某段 | 不行（只能整目录 retry） | 可以 `--stage b` |
| 墙钟 | 一个 walltime 要盖住全部段 | 每段各有一个 |
| 轨迹闸门 | 只有"异常退出/未正常收尾/CONTCAR 残缺"的粗判 | `relax_diagnose` 的 thrown/electronic/oscillating 细判 |

**丢掉的那一项要认**：`ck_relax_skip` 里那套"甩飞/撞 NELM/大幅震荡就拦在本段、
不放行下一段"的诊断，在 in_job 下没有等价物——bash 里做轨迹分析不现实。
代价是病态结构可能连着跑完三段才被发现，多烧一段的机时；换来的是少排两次队。
在你们这种排队时间远大于单段机时的集群上，这笔账是划算的，但如果某个体系反复
震荡，就临时把它切回 `STAGE_MODE="tf_stages"` 单独调。

`run_relax.sh` 由 `STAGE_SPEC` 驱动生成（不再写死 ISIF=2→3→IBRION=1），
沿用 elastic-dft-cpu 那版的看门狗（OUTCAR 停滞 `STALL_MIN` 分钟判卡死）、每段
`OUTCAR.sN/OSZICAR.sN/CONTCAR.sN` 存档、`.sN.started` 中断续跑。

配套判据在 `checks_relax.py`（`check: relax_injob`）：收敛看 OUTCAR 总闸，
未收敛时从 `.sN.done` 数进度并区分"跑完没收敛 / 某段中断"，同时兼容老材料
遗留的 `step1{a,b,c}_*` 目录。

## 验证记录

* `tf_stages` 模式：与原 `skill/band-dft-cpu/gen_step1_PBE_opt.py` 在同一 2D 结构上
  输出逐字节相同（只多一行 `STAGE_MODE=tf_stages`）。
* `in_job` 模式：生成 `INCAR.s1_a/s2_b/s3_c` + `run_relax.sh`，
  s1 已删除 `IOPTCELL`（ISIF=2 段不该有面内约束）、s2/s3 保留；
  `submit.sh` 的 VASP 执行行已换成 `bash run_relax.sh`；`bash -n` 语法通过。
* `checks_relax.py`：对"没跑/1 段完成/某段中断/跑完未收敛/已收敛"五种状态
  各返回了正确的 (done, note)。
* 0D 分支：Li@C60 走 `mol_common`，`ISPIN=2`（243 价电子为奇数）、`DIPOL` 取
  几何中心；带 `FUNC + MOL_* + STALL_MINUTES` 的 step.conf 一次解析全部生效
  （`STALL_MINUTES=30` 正确落到 run_relax.sh）。
* 0D 守卫：同一个 Li@C60 用 `MOL_BRANCH=False` 的薄壳跑，报错信息指出
  "本技能没有开 0D 支持"而不是模板缺失。
* elastic-dft-cpu 薄壳（`CELL_POLICY="standard"`）在装了 pymatgen 的环境里跑通，
  标准化 + 三段 INCAR + run_relax.sh 都正确。

## "薄壳"是什么

指技能里那个 `gen_step1_*.py`：迁移后它不再包含任何计算逻辑，只剩
`import relax_common` + 一次 `R.run(...)` 声明本技能的策略，十来行。
所有真正干活的代码（模板渲染、维度判定、磁性/U/LMAXMIX 判定、分段、
0D 分支）都在池子里，改一次全技能生效。壳薄到什么程度：band-dft-cpu 那个从
1083 行变成 22 行。

## 四个技能的策略对照

| | band-dft-cpu | elastic-dft-cpu | ke-dft-cpu | kl-dft-cpu |
|---|---|---|---|---|
| `OUTDIR_SINGLE` | step1_PBE_opt | step1_std_opt | step1_std_opt | step1_std_opt |
| `CELL_POLICY` | primitive | standard | standard | standard |
| `VACUUM_AXIS_POLICY` | error | rotate | rotate | rotate |
| `MOL_BRANCH` | **True** | False | False | False |
| `NEXT_STEP` | gen_step2_static.py | gen_step2_elastic.py | gen_step2_static.py | gen_step2_static.py |

`STAGE_MODE` 四个都是 `in_job`。elastic-dft-cpu 原来"3D 恒为单段"，迁移后 3D 也分段；
想保持旧行为传 `STAGE_MODE="single"`。

## 0D 支持到哪一步了

| | 0D 能不能跑 | 说明 |
|---|---|---|
| band-dft-cpu step1 | ✅ | `MOL_BRANCH=True` → mol_common，需要 `incar_0d.tpl` / `submit_std_0d.tpl` |
| band-dft-cpu step2 | ⚠️ 要补 | `dim_common` 池子版已放行 `DIM=0D`，但还缺 `incar_0d`/`submit_std_0d` 的 step2 版；KPOINTS 要走 `--no-vaspkit` 复用 step1 的 Γ |
| band-dft-cpu step3/step4 | ❌ 不该跑 | 高对称路径定义在晶体倒空间，分子没有能带 |
| elastic-dft-cpu / ke-dft-cpu / kl-dft-cpu | ❌ 不该跑 | 弹性张量、晶格热导、形变势对孤立分子都没有定义 |

**这不是"还没支持"，是物理上不该跑**，所以 `MOL_BRANCH=False` 的技能碰到
0D 结构会直接报错并说明原因，而不是默默跑出一个数。守卫在
`resolve_dimension()` 里——如果不拦，后面 `resolve_tpl` 只会抛一句
"找不到 incar_0d.tpl"，看不出真正的问题在哪。

## 一个必须知道的 step.conf 约束

`stepconf.StepConf.__init__` 对 `[params]` 里**没在 spec 里声明过的键直接报错**。
所以池子里所有读 step.conf 的模块必须共用一份 `CONF_SPEC`、只解析一次：
`relax_common.load_step_params()` 解析后存进 `STEP_PARAMS`，`mol_common`
从那里取 `MOL_*`。早期版本里 mol_common 自己 `stepconf.load(CONF_SPEC_MOL)`，
一旦项目的 step.conf 里有 `FUNC` 就会炸——这个坑已经填了，但**将来往池子里
加新模块时同样要遵守**：新参数加进 `relax_common.CONF_SPEC`，不要新开一份。

另一个相关的：`stepconf.load` 会校验 step.conf 里的 `STEP=` 与调用方声明的
步骤名是否一致。改成 in_job 之后步骤名从 `step1a_PBE_opt` 变成 `step1_PBE_opt`，
项目里写死了 `STEP=step1a_PBE_opt` 的那几份 step.conf 要跟着改。

## 迁移步骤

1. 打 `tf_common_pool.patch`（两处，共 8 行）。
2. `skill/_common/` 放入本目录的文件。
3. 逐技能删副本：`rm skill/band-dft-cpu/dim_common.py skill/band-dft-cpu/check_common.py
   skill/band-dft-cpu/stepconf.py`（`gen_need` 不动）→ 跑一次 `tf ... gen` 确认远端
   拿到的文件 md5 与池子一致 → 再删下一个技能的。
4. `skill/band-dft-cpu/gen_step1_PBE_opt.py` 换成薄壳，`gen_need` 里加 `relax_common.py`
   （薄壳 import 它；gen 脚本本身 tf 总是覆盖推送，但它 import 的模块要进清单）。

## elastic-dft-cpu / ke-dft-cpu / kl-dft-cpu 的迁移

三个技能的 step1 换成薄壳即可，差别只在策略声明：

```python
R.run(OUTDIR_SINGLE="step1_std_opt",
      SCRIPT_NAME="gen_step1_std_opt.py",
      NEXT_STEP="gen_step2_elastic.py",
      STAGE_MODE="in_job",
      CELL_POLICY="standard",          # 弹性张量定义在惯用晶轴上
      STD_CELL="primitive_standard",
      MOL_BRANCH=False)
```

要注意的两点：elastic-dft-cpu 原来只对 2D 分段、3D 恒为单段 ISIF=3，迁移后 3D 也会分段
（想保持旧行为就传 `STAGE_MODE="single"`）；另外 elastic-dft-cpu 的 step1 还有
"真空轴自动轮换到 c"（`rotate_vacuum_to_c`）这一步，公共池目前没有，
迁移时要么一并搬进 `relax_common`，要么先留在 elastic-dft-cpu 的薄壳里做前置处理。
