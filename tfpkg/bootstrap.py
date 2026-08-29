# -*- coding: utf-8 -*-
"""bootstrap —— 配置/发现流（00_state+01_yamlmini+02_skills+03_projects+04_discover 合并）。
常量/配置模板/标准库 import + YAML 加载 + 技能发现/装配 + 项目配置合并 + 本地材料发现。
对外接口：load_config / discover_skills / apply_skills / merge_project_configs / discover_local 等。"""

import os
_PKG_DIR = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))
_PKG_ROOT = os.path.normpath(os.path.dirname(_PKG_DIR))

# ===== 来自 00_state.py =====
# -*- coding: utf-8 -*-
"""00_state —— 全局常量 / 配置模板 / 可变缓存 / 标准库 import。

对应原单体 tf 的「文件头 import + 所有模块级常量」。由 tfpkg/__init__.py
的装配器最先执行，把标准库 import 与全部常量注入单一命名空间，供后续
分片直接按名字使用。注意：COLLECTOR 已独立成 tfpkg/_collector_remote.py。
"""
import argparse
import base64
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

# ===== TF_VERSION (原 L731-L731) =====
TF_VERSION = "1.0"

# ===== CONFIG_SEARCH (原 L735-L743) =====
CONFIG_SEARCH = [
    "./tf.yaml", "./tf.yml", "./tf.json",
    os.path.join(_PKG_DIR, "tf.yaml"),
    os.path.join(_PKG_ROOT, "tf.yaml"),
    os.path.join(_PKG_ROOT, "setting", "tf.yaml"),  # v1.0：也可放 setting/tf.yaml
    os.path.expanduser("~/.config/taskflow/tf.yaml"),
    os.path.expanduser("~/.tf.yaml"),
]

# ===== EXAMPLE_CONFIG (原 L745-L785) =====
EXAMPLE_CONFIG = """\
# ===== taskflow v3 全局配置 =====
host: jzzn                 # 默认 ssh 别名（可被项目 project_setting/hpc.yaml 的 ssh_host 覆盖）
# user: wangchao           # squeue 查询用户，缺省 = 远端当前用户

# 项目根列表：tf 扫描每个根下的 project_setting/tf_*.yaml（含一层子目录，
# 如 C20/project_setting/tf_C20.yaml）。配置跟着项目走，加新项目 = 在项目里
# 放一份 project_setting/tf_<项目名>.yaml（tf -p X init 可生成），不用改这里。
project_roots:
  - /home/wangchao/Fullerene_Network

# task_types：只写站点相关覆盖。技能的 steps / gen_need / aux_files 由
# skill/<技能>/skill.yaml 自描述，tf 启动时自动发现（tf skills 查看），
# 这里不用再抄一遍。key 就是 -tt 用的短名，等于技能名。
task_types:
  band:
    work_dir: /public/home/wangchao/Fullerene_Network/work
                           # 超算工作根：远端目录 = work_dir + 材料相对路径
    # hpc: jzzn            # 覆盖清单里的默认集群
    # plot_steps: false    # 关掉该技能清单里 optional_steps 的画图步骤组
    # run_steps: [1, 2]    # 只跑部分步骤（序号 = 清单里的 seq）
    # max_jobs: 20         # 该技能「同时提交」的超算作业上限；达到后新任务本地待命，
                           # 等 watch 拉到有空位（有作业算完）再自动补交。不写 = 不限。
    # hang_check: true     # 挂死作业自动恢复（v1.11，默认开）：进度指纹判定挂死——(OUTCAR
                           # 字节数, OSZICAR 行数) 连续 hang_min_stale_rounds 轮不变且输出年龄
                           # 超 hang_stale_secs 才算（指纹在涨/SCF rms 在降 = 活着，不判）。
                           # 判定后按原因处理：SCF 空转自动升级 INCAR（hang_fix_scf 时补 AMIX/BMIX
                           # → ALGO=All → NELM≥200，原子写+备份）；NODE_FAIL 直接重跑；磁盘满
                           # 只告警。恢复：scancel（等退出）→ 校验 CONTCAR → 续跑重交，升级后给
                           # hang_grace_rounds 轮宽限期。hang_dry_run: true 只打印不动手（观察期）。
    # steps: [...]         # 逃生口：写了就完全接管清单里的步骤定义
  elastic:
    work_dir: /public/home/wangchao/Fullerene_Network/work

# 技能开关与搜索路径（都可选）
# enabled_skills: [band, elastic]     # 白名单，写了就只启用这些
# disabled_skills: [kl]               # 黑名单
# skill_paths: [~/my-tf-skills]       # 追加技能搜索路径

# 老模式也支持：类型里写 root（= 超算目录，远端发现材料）而不写 local_root，行为同 v2.x
"""

# ===== DEFAULT_PROJECT_CONFIG (原 L788-L810) =====
DEFAULT_PROJECT_CONFIG = """\
# 项目配置：tf_<项目名>.yaml（文件名全局唯一，禁止与其他项目重复）
# 本文件放在项目文件夹的 project_setting/ 下；local_root 缺省 = project_setting 的父目录。
# 未写的字段自动继承全局 tf.yaml 里同 key 类型的定义。
task_types:
  band:
    desc: 能带计算
    work_dir: /public/home/wangchao/Fullerene_Network/fullerene_network/test
    local_root: ".."       # ".." = 体系根（如 Fullerene_Network）：材料名带 C20/ 前缀，
                           # 超算目录 = work_dir/C20/qHPC20（与本地树一致，推荐）
                           # 缺省 "." = 项目目录本身：材料名 qHPC20，超算 = work_dir/qHPC20
    # skill_dir: skill/band  # 缺省继承全局；项目有专用脚本时写相对本目录的路径
    # hpc: jzzn
    # plot_steps: false    # 能带画图步骤开关，默认开（true）。开 = step3/4 算完后
                           # 自动追加 S3.1_plot/S4.1_plot 两步：在材料目录运行
                           # gen_step3.1/4.1_plot_band.py（不提交 SLURM），生成
                           # step3_band_plot/step4_band_plot（band.png/band.dat/
                           # band_summary.json），状态显示 completed/not started/
                           # error，产物自动整目录拉回 result/。写 false 关闭这两步
    # gen_need: [dim_common.py, incar_2d.tpl, incar_3d.tpl, submit_std_2d.tpl, submit_std_3d.tpl, submit_ncl_2d.tpl, submit_ncl_3d.tpl]
    # steps:               # 缺省继承全局；项目流程不同就写自己的
    #   - {name: step1_PBE_opt, label: S1_opt, check: outcar_relax, gen: gen_step1_PBE_opt.py}
"""

# ===== DEFAULT_HPC_SETTING (原 L812-L823) =====
DEFAULT_HPC_SETTING = """\
# 超算配置（创建项目时由 tf init 复制为 project_setting/hpc.yaml，按项目改）
name: jzzn                 # 显示在状态表 hpc 列
ssh_host: jzzn             # 该超算的 ssh 别名
# 模板映射：gen 要的逻辑名 -> skill 目录里的实际模板文件（2D/3D 各一套，
# 维度由 gen 脚本自动判定）。换超算 = 改这里（如 submit_hf_vaspstd_2d.tpl）
template_map:
  submit_std_2d.tpl: submit_jzzn_vaspstd_2d.tpl
  submit_std_3d.tpl: submit_jzzn_vaspstd_3d.tpl
  submit_ncl_2d.tpl: submit_jzzn_vaspncl_2d.tpl
  submit_ncl_3d.tpl: submit_jzzn_vaspncl_3d.tpl
"""

# ===== DEFAULT_PROJECT_SETTING (原 L825-L842) =====
DEFAULT_PROJECT_SETTING = """\
# 项目设置（就近优先：从材料目录向上找最近的 project_setting）
# v1.9.9：新建的技能默认【不参与自动推进】——init 只是把配置建好，
# 不等于你想现在就算它。想让本技能跟着 auto_advance 自动开算，改成 true：
#     tf -tt <技能> -p <材料> auto on      （或直接把下面这行改 true）
# 手动 tf start / retry / rerun 不受本开关影响，随时可以单独跑。
auto_advance: false
# 路径占位符：{matdir}=材料目录 {mat}=材料名 {root}=本地项目根
base_dir: "{matdir}"
result_dir: "{matdir}/result"     # tf fetch 回拉目的地（每步一个子目录）
log_dir: "{matdir}/log"           # 该项目的操作日志 tf.log
# work_dir 缺省继承项目配置 tf_<项目名>.yaml 里的类型定义；只在要为这个
# 项目单独换超算工作根时才取消注释（优先级高于类型配置）：
# work_dir: /public/home/wangchao/Fullerene_Network/fullerene_network/test
# fetch 回拉的文件清单：v1.11 起由技能 skill.yaml 的 fetch_files 自描述；
# 项目要覆盖时再取消注释、写自己的清单（否则继承技能的 fetch_files）。
# fetch_files: [INCAR, POSCAR, POTCAR, KPOINTS, KPOINTS_OPT, kpath.json, submit.sh, OUTCAR, CONTCAR, EIGENVAL, vasprun.xml, queue.out]
"""

# ===== SKILL_MANIFEST (原 L1091-L1091) =====
SKILL_MANIFEST = "skill.yaml"

# ===== SKILL_SCHEMA_MAX (原 L1092-L1092) =====
SKILL_SCHEMA_MAX = 1

# ===== COMMON_POOL_DIR (原 L1095-L1095) =====
COMMON_POOL_DIR = "_common"

# ===== _MANIFEST_TYPE_KEYS (原 L1098-L1101) =====
_MANIFEST_TYPE_KEYS = ("desc", "steps", "optional_steps", "gen_need", "aux_files",
                       "gen_dir", "plot_steps", "run_steps", "dir_name",
                       "skill_subdir", "hpc", "work_dir", "root",
                       "template_dir", "template_layout", "fetch_files")

# ===== _PS_CACHE (原 L1640-L1640) =====
_PS_CACHE = {}

# ===== _WARN_WORKDIR (原 L1703-L1703) =====
_WARN_WORKDIR = set()   # v1.11：work_dir 未显式指定时按技能去重的提示

# ===== _LOCAL_ONLY_STEP_KEYS (原 L1875-L1877) =====
_LOCAL_ONLY_STEP_KEYS = {"gen", "gen_need", "aux_files", "run", "group", "seq",
                         "contcar_to_poscar", "fetch_all", "fetch_files", "after",
                         "src"}   # v1.5 src

# ===== SCANCEL_MARK (原 L2157-L2157) =====
SCANCEL_MARK = ".tf_scancel.json"   # lpath 下的"tf stop 取消"标记（v1.4）

# ===== _MAX_INFLIGHT_DEFAULT (原 L2246-L2246) =====
_MAX_INFLIGHT_DEFAULT = int(os.environ.get("TF_MAX_INFLIGHT", "6") or 6)

# ===== _BUSY_KINDS (原 L2247-L2247) =====
_BUSY_KINDS = ("R", "PD")   # OTHER=作业已完成/失败（job state 非 R/PD），不算并发占用

# ===== _MAX_JOBS_DEFAULT (原 L2280-L2280) =====
_MAX_JOBS_DEFAULT = os.environ.get("TF_MAX_JOBS")   # 字符串或 None

# ===== REASON_MAX (原 L2522-L2522) =====
REASON_MAX = 8      # 原因最多显示几个字符，超出直接截掉

# ===== _SKILL_ONLY (原 L2915-L2915) =====
_SKILL_ONLY = False      # v1.9：rerun --from-skill 时置 True，忽略项目侧模板覆盖

# ===== STEP_CONF (原 L2996-L2996) =====
STEP_CONF = "step.conf"

# ===== _STEPCONF_MOD (原 L2997-L2997) =====
_STEPCONF_MOD = {}

# ===== _DIM_MOD (原 L3129-L3129) =====
_DIM_MOD = {}

# ===== FAN_JOBIDS (原 L3369-L3369) =====
FAN_JOBIDS = {}   # 代表 jobid → 该扇出步骤的全部 jobid（scancel 时展开）

# ===== _AUTO_CASCADE_MAX (原 L4368-L4368) =====
_AUTO_CASCADE_MAX = max(1, int(os.environ.get("TF_AUTO_CASCADE", "8") or 8))

# ===== FETCH_STAMP (原 L5088-L5088) =====
FETCH_STAMP = ".tf_fetched"   # result_dir/<step>/ 下的"已抓取"戳记（v1.11）

# ===== _MAT_DIR_CACHE (原 L5420-L5420) =====
_MAT_DIR_CACHE = {}   # root -> [(relpath, basename, dir)]，resolve_mat_dir 扫盘缓存

# ===== _RESOLVE_DISC_CACHE (原 L5438-L5438) =====
_RESOLVE_DISC_CACHE = {}

# ===== _LEVEL_ALIAS (原 L6036-L6039) =====
_LEVEL_ALIAS = {"pbe": "pbe", "pbesol": "pbe", "gga": "pbe", "3": "pbe",
                "step3": "pbe", "s3": "pbe",
                "hse": "hse", "hse06": "hse", "4": "hse",
                "step4": "hse", "s4": "hse"}

# ===== _LEVEL_DESC (原 L6041-L6044) =====
_LEVEL_DESC = {
    "pbe": "只算到 step3（PBE/PBEsol 级别；具体泛函仍由 step.conf 的 FUNC 决定，出厂 pbesol）",
    "hse": "继续算到 step4（HSE06）",
}

# ===== _LEVEL_HEADER (原 L6046-L6046) =====
_LEVEL_HEADER = "# step.conf —— 本材料共用参数（BANDGAP 由 tf level 维护）\n\n[params]\n"

# ===== USAGE (原 L6489-L6653) =====
USAGE = """\
用法:
  tf [选项] [ROOT] [命令]

命令:
  status    状态总表（缺省命令）；加 -p 看单材料详情
  list      状态总表（只读）：不 auto-fetch、不 auto-advance，纯查看、绝不提交。
            默认复用 TF_CACHE_TTL 秒内的本地采集缓存（跳过 ssh，秒开）；
            --refresh 强制重新采集
  summary   极简汇总（只读，省 token）：每任务类型一行计数 + FAIL 清单。
            巡检首选：无异常时输出只有几行；有 FAIL 再按清单单点深挖。
            同 list，默认走本地缓存，--refresh 强制刷新
  start     开始/提交：输入没生成先 gen 再 sbatch。无 -p = 一键推进全部材料。
            ★ 唯一会向超算提交作业的命令（init/retry/rerun 都只生成不提交）
  stop      取消作业。无 -p = 一键停止全部作业（有确认）；-p = 该材料全部作业；-p -job = 指定步骤。
            取消的步骤打 scancel 标记：状态列显示 scancel，auto_advance
            和批量 start 都不会再动它；重跑：-p X start（保留文件直接重交）/
            retry / rerun，或跨材料 -status scancel start（retry/rerun）
  retry     只重新生成输入，不提交——保留已有产物（OUTCAR/CONTCAR），检查后用 start 提交。
            适合超算上手改文件/改参数后。无 -p = 一键重生成所有 FAIL 步骤
  rerun     推倒重来，只生成不提交（待 start）：整材料 = 清空整个工作目录（只留
            POSCAR）从第一步重生成；-j 指定 = 只删该步目录重生成。无 -p 无 -j =
            全部材料整体重来；-j 不带 -p = 全部材料只重做该步骤（有确认）
  clean     只删不建，回到 PREP：无 -p = 全部材料清空（本地+超算只留 POSCAR）；
            -p X = 该材料清空；-p C20 = 该体系所有材料；-p X -j Y = 只删该步骤目录。
            关联作业一并取消（有确认，-y 跳过）
  auto      一键开关自动提交：tf auto on 开 / tf auto off 关 / 无参看当前。
            改写全局 tf.yaml 的 auto_advance；关后 status/watch 只看不提交，
            手动 start/retry/rerun 不受影响（动目录、恢复备份前先 off）
  hpc       把 -p 指定的项目分配到指定超算（未指定的项目一律不动）：
            tf -p X,Y hpc 集群名              材料级（该材料全部技能生效）
            tf -tt elastic -p X,Y hpc 集群名  只改 elastic 技能（写 材料/elastic/hpc.yaml）
            集群主配置 = 包内 setting/<名>.yaml（照 jzzn.yaml 建 name/ssh_host/
            template_map）；模板文件放 skill/<技能>/。状态表 hpc 列按技能显示
  adopt     接管手工整理的技能子目录（须 -tt）：人手工把 POSCAR、
            project_setting、result、log 搬进了 材料/<技能>/ 时用——
            POSCAR、project_setting 挪回材料根（多技能共用，必须在根），
            result/log 留在技能目录；随后自动做 migrate-subdir：远端 step*
            移进技能目录、项目配置开 skill_subdir。有作业在跑的跳过，
            算完再跑一次。--dry-run 先看计划，-y 免确认
  migrate-subdir  把该技能已完成材料迁进技能子目录（须 -tt）：
            远端 step* → work/材料/<技能>/step*，本地 result、log → 材料/<技能>/，
            项目配置自动开 skill_subdir。批量只迁全完成的；在跑的自动跳过；
            -p X 可强制迁单个（无作业即可）。--dry-run 先看计划，-y 免确认
  dir       只输出目录路径：-tt 类型根 / -p 材料根 / -p -job 步骤目录
  init      初始化：不带 -p = 当前目录下所有材料批量生成 project_setting/
            （每个材料目录一份）；-p X = 只初始化该材料；-p X -j Y = 只生成
            该步骤输入文件（gen），不提交——提交前可先检查。
            配 -tt = 给已有项目追加该技能类型段（如 band 项目里
            tf -tt elastic init 追加 elastic 段，并建技能子目录）
  fetch     手动强制拉回结果（status 时已自动保存完成的步骤到 result/，
            项目 setting.yaml 里 auto_fetch: false 可关闭自动保存）
  watch     监控模式：每 -i 秒（默认 300）自动刷新状态、auto-fetch 算完步骤、
            auto-advance 提交下一步；挂死作业自动恢复（hang_check，默认开，见 5.6）；有变化才打印总表；Ctrl+C 退出。
            每轮自动检测配置文件改动（tf.yaml、project_setting/*.yaml、
            材料或技能的 hpc.yaml）并重载——改配置不用重启监控。
            加 -d 放后台运行（日志 .tf_watch.log，不占用终端），
            tf watch --stop 停止后台监控（任意目录可执行）。
            零输入全自动：tf.yaml 里 auto_watch: true（任何 tf 命令顺带拉起
            监控）+ tf watch --install（crontab 保活，重启后自动恢复，
            --uninstall 移除）
  json      输出 JSON
  config    打印示例配置
  help      显示本帮助

选项:
  -tt TT          任务类型（band/elastic/自定义，配置里 task_types 的 key）；
                  不指定时 -p 在所有类型里解析，跨类型重名会提示补 -tt
  -p MAT          指认 project：完整名（C20/qHPC20）或唯一 basename（qHPC20）；
                  多个项目用逗号分隔（-p A,B）或直接跟在后面（-p A B retry）
  -j, -job STEP   指认 job（步骤名/label/1 起始序号）。配 -p = 该材料的该步骤；
                  start/stop/retry/rerun/clean 可不带 -p = 全部材料只操作该步骤；
                  多个步骤逗号分隔（-j A,B）或直接跟在后面（-j A B start）
  -x, --exclude   跳过指定项目（逗号分隔，全名或 basename），任何命令可用
  -status ST      只保留含指定状态步骤的材料：done/running/pd/error/
                  waiting/scancel，逗号分隔，对任意命令生效。如：
                  tf -status scancel            只看被 stop 取消的
                  tf -status scancel start      把它们全部重跑（保留文件直接重交）
                  tf -status error retry        重交全部失败步骤
  -c, --config    配置文件（缺省找 ./tf.yaml、~/.config/taskflow/tf.yaml、~/.tf.yaml）
  --host HOST     覆盖配置里的 ssh 主机
  -u, --user      squeue 查询用户
  -f, --force     已有作业时先 scancel 再提交；start 一个 FAIL 步骤；前序缺失强制执行
  --all           fetch 时拉回每个步骤的全部文件（不只 fetch_files 清单）；
                  配合 -tt 可只拉某个技能：tf -tt elastic fetch --all
  --hide-done     状态表隐藏全部步骤都完成的项目（配置 hide_done: true 默认开，
                  --show-done 临时取消）
  --diff          summary 与上次快照对比，无变化不输出（省 token，巡检用）
  --refresh       list/summary 强制跳过本地状态缓存，重新 ssh 采集
  -i, --interval  watch 刷新间隔秒数（默认 300）
  -y, --yes       stop/rerun 免确认
  -V, --version   显示版本号
  -h, --help      显示帮助（同 tf help）

规则:
  同一任务类型下项目名不允许重复（启动即报错）；不同类型下允许同名。
  状态词: done 完成 | running 运行中 | pd 排队 | error 未通过判据 | waiting 未开始
        | scancel 被 tf stop 取消（打标记，auto 不会重跑；显式 start/
          retry/rerun 重跑后标记自动清除）
  表格 hpc 列 = 该项目所用超算，dim 列 = 2D/3D 判定；每个项目两行：
  第一行 = 各步骤状态词，第二行 = 节点/任务号/已跑时间（排队时为任务号+原因）。
  同 group 的步骤合并为一列（如三段式弛豫 S1_relax；第二行显示走到哪段）。
  本地模式（类型配置 local_root）：输入文件以本地项目目录为准（含 POSCAR），
  project_setting/ 就近优先（tf_<项目名>.yaml = 项目配置，setting.yaml 定路径，
  hpc.yaml 定超算与模板映射），超算只负责计算，目录树 = work_dir + 项目相对路径。
  配置跟着项目走：全局 tf.yaml 只需 project_roots 列出项目根；多个项目配置
  定义同一类型 key = 该类型的多个分段（local_root 不同），表格合并显示。
  能带画图步骤：step3/4 算完后自动追加 S3.1_plot/S4.1_plot（材料目录运行画图
  脚本，不交 SLURM），状态 = completed/not started/error，产物整目录自动拉回。
  类型配置 plot_steps: false 关闭（备注见 tf config 输出的模板）。
  带隙层级（ke/band）：材料 project_setting/templates/step.conf 里
  BANDGAP = pbe|hse（默认 hse）。pbe = 只算到 PBE 带隙+画图，跳过整段 HSE；
  hse = 继续 HSE。改这一个键即增删 HSE 步骤，无需改 skill.yaml。
  按材料改每步输入：编辑 project_setting/templates/<步骤名>/ 下的 incar_*.tpl、
  submit_*.tpl（只影响本材料；缺时回落技能库出厂模板）。tf init 从技能库铺一套。
  技能子目录：类型配置 skill_subdir: true 时，该技能的计算放进材料目录下的
  技能子文件夹（远端 work/材料/<dir_name>/stepN，本地 result 在 材料/<dir_name>/result），
  dir_name 缺省 = 类型 key（想叫别的名字就写 dir_name: 名字）。同一材料可同时
  挂多个技能（项目配置里写多段），各技能的目录互不干扰；平铺结构的老项目
  不要直接开（会被当新材料），迁移方法见 tf.yaml band 块注释。
  不同技能跑不同超算：把 project_setting/hpc.yaml 复制为
  材料/<技能>/hpc.yaml 再改字段（ssh_host/template_map/队列）即可，
  优先级高于材料级 hpc.yaml；材料/<技能>/ 下的同名模板也最优先被用。
  按技能限制并发提交：task_types.<key>.max_jobs: N（全局 max_jobs 兜底，
  TF_MAX_JOBS 环境变量再兜底）。上限只卡「提交超算（sbatch）」，不卡本地
  生成输入：达到上限后新任务先本地生成输入（变 TODO）待命，watch 每轮拉状态
  发现有空位（有作业算完）就自动补交；手动 tf start 同样只卡提交不卡生成。

示例:
  tf                                   全部状态总表
  tf -tt band                          只看能带类
  tf -tt band migrate-subdir           band 已完成材料迁进 band/ 子目录
  tf -tt elastic start                 开始 elastic 技能全部材料
  tf -tt elastic -p Ela1 start         只开始 elastic 的 Ela1（指定材料）
  tf -p qHPC20 status                  单材料详情（basename 即可）
  tf -p C20/qHPC20 -job 1 retry        用现有文件重交第 1 步（完整名也行）
  tf -p qHPC60 qTP1C60 retry       同时重交多个项目（用超算上已改好的输入直接提交）
  tf -tt band -p qTPC24 -job S2_static start
  tf -p qHPC20 stop                    取消该材料所有作业（有确认，打 scancel 标记）
  tf -status scancel                   只看被 stop 取消的材料
  tf -status scancel start             把它们全部重跑（保留文件直接重交）
  tf auto off                          暂停自动提交（动目录/恢复备份前先做）
  tf auto on                           恢复自动提交
  tf -tt band adopt --dry-run          接管手工整理的 band/ 目录：先看计划
  tf -tt band adopt -y                 执行接管（POSCAR/配置回根 + 远端迁入 band/）
  tf -tt elastic -p qHPC20 hpc tianhe  qHPC20 的 elastic 技能切到 tianhe 集群
  tf -p qHPC20 -job 1 dir              输出该步骤在超算的目录路径
  ssh jzzn "cd $(tf -p qHPC20 -job 1 dir) && tail -50 OUTCAR"
  tf start                             一键推进所有材料（提交）
  tf list                              只读状态总表（不提交、不拉取）
  tf stop -y                           一键停止全部作业
  tf retry                             一键重生成所有 FAIL 步骤输入（不提交，再 start）
  tf rerun                             全部材料清空重生成（有确认，不提交）
  tf -j S3_WAVECAR rerun -y            全部材料只重做 S3（本地最新脚本重生成，不提交）
  tf -j S3_WAVECAR -x qHPC60 rerun -y  同上但跳过 qHPC60
  tf -j S2_static start                全部材料只提交 S2 步骤
  tf -j S3.1_plot S4.1_plot start      全部材料只跑两个画图步骤
  cd ~/Fullerene_Network && tf init    批量初始化目录下所有项目
  tf -p C20/qHPC20 init                只初始化 C20 这一个项目
  tf -p qHPC20 fetch                   拉回该材料各步骤结果到 result/
  tf fetch                             拉回全部材料结果
  tf clean                             全部材料清空（留 POSCAR）
  tf -p C20 clean                      清空 C20 体系所有材料
  tf -p qHPC20 -j 1 clean              只删 S1_opt 步骤目录
"""

# ===== STATUS_ALIAS (原 L6790-L6803) =====
STATUS_ALIAS = {   # v1.4 -status 选项的状态词 → 内部 kind
    "done": "OK", "ok": "OK", "completed": "OK",
    "running": "R", "run": "R", "r": "R",
    "pd": "PD", "pending": "PD", "queue": "PD", "queued": "PD",
    "error": "FAIL", "fail": "FAIL", "failed": "FAIL",
    "waiting": "WAIT", "wait": "WAIT",
    # patch_cell_word：新状态词。ready 是"依赖已齐可启动"，映射到 TODO+PREP；
    # blocked 是"被依赖卡住"，等价于原来的 waiting/WAIT。
    "blocked": "WAIT", "block": "WAIT",
    "ready": "TODO+PREP", "actionable": "TODO+PREP",
    "todo": "TODO", "prep": "PREP", "other": "OTHER",
    "scancel": "SCANCEL", "scancelled": "SCANCEL", "cancel": "SCANCEL",
    "cancelled": "SCANCEL", "canceled": "SCANCEL",
}

# ===== WATCH_PID (原 L6842-L6842) =====
WATCH_PID = ".tf_watch.pid"     # watch 后台模式的 pid 文件名

# ===== WATCH_LOG (原 L6843-L6843) =====
WATCH_LOG = ".tf_watch.log"     # watch 后台模式的日志文件名

# ===== JSON_SCHEMA（v2.0 新增，tf json --schema 用）=====
JSON_SCHEMA = """\
任务状态 JSON 输出结构（schema_version 2）

顶层字段：
  schema_version : int           本 schema 版本号
  tf_version     : str           taskflow 版本
  types          : [type]        任务类型（技能）列表

type 对象：
  key       : str      技能名（对应 -tt）
  root      : str      项目根目录
  materials : [material]

material 对象：
  name     : str       材料名（如 C20/qHPC20）
  path     : str       材料目录
  steps    : [step]    步骤列表
  active   : step|null 当前活动步骤对象（全部完成时为 null）
  action   : str       建议动作（retry/start/... 或 -）
  hpc_name : str       实际运行的超算名（如 3090/jzzn）
  host_eff : str       实际 ssh 别名
  dim      : str       维度（如 3D / 2D）

step 对象：
  name  : str      步骤名（label）
  label : str      显示文本
  kind  : str      状态：OK=完成 R=运行 PD=排队 FAIL=失败 TODO=待提交
                    PREP=未生成 WAIT=阻塞 SCANCEL=已取消
  diag  : str      诊断信息（FAIL 时给出原因）
  job   : {id,state,info} | null    作业信息
"""

# ===== 来自 01_yamlmini.py =====
# -*- coding: utf-8 -*-
# 01_yamlmini —— 迷你 YAML 解析器 + load_config 配置加载
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L849  _yaml_strip_comment
#   L861  _yaml_split_top
#   L883  _yaml_scalar
#   L914  _flow_depth
#   L932  _mini_yaml
#   L1058  load_config

# 注：YAML 解析器已抽成真实深模块 tfpkg/yamlmini.py，
# 由 __init__.py 导入后把 _mini_yaml 注入共享命名空间；
# 本文件只保留 load_config（依赖 CONFIG_SEARCH 常量）。

# ===== load_config (原 L1058-L1080) =====
def load_config(path):
    from tfpkg import _mini_yaml
    if path is None:
        for c in CONFIG_SEARCH:
            if os.path.isfile(c):
                path = c
                break
    if path is None:
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        sys.exit("错误：配置文件 %s 读取失败（%s）。" % (path, e.strerror or e))
    if path.endswith(".json"):
        return (json.loads(text) or {}), path
    try:
        import yaml
        return (yaml.safe_load(text) or {}), path
    except ImportError:
        try:
            return (_mini_yaml(text) or {}), path
        except ValueError as e:
            sys.exit("错误：本机 python 没有 PyYAML，内置解析器又报错：%s" % e)

# ===== 来自 02_skills.py =====
# -*- coding: utf-8 -*-
# 02_skills —— 技能发现 / skill.yaml 清单 / 技能装配
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1104  skill_search_dirs
#   L1124  _load_manifest
#   L1182  discover_skills
#   L1201  _merge_type
#   L1217  apply_skills
#   L1238  _seq_sort_steps
#   L1251  expand_optional_steps
#   L1304  _seq_key
#   L1314  _name_seq
#   L1321  step_seq
#   L1329  skill_checks_for
#   L1346  cmd_skills

# ===== skill_search_dirs (原 L1104-L1121) =====
def skill_search_dirs(cfg):
    from tfpkg import _PKG_ROOT
    """技能搜索路径，靠前优先（同名技能先命中者生效）。"""
    pkg_root = _PKG_ROOT
    cdir = cfg.get("_config_dir") or ""
    cands = ["./skill"]
    cands += [os.path.expanduser(str(p)) for p in (cfg.get("skill_paths") or [])]
    if cdir:
        cands += [os.path.join(cdir, "skill"),
                  os.path.normpath(os.path.join(cdir, "..", "skill"))]
    cands += [os.path.join(pkg_root, "skill"), os.path.expanduser("~/.tf/skill")]
    out, seen = [], set()
    for d in cands:
        rd = os.path.realpath(d)
        if rd not in seen and os.path.isdir(rd):
            seen.add(rd)
            out.append(rd)
    return out

# ===== _load_manifest (原 L1124-L1179) =====
def _load_manifest(path):
    from tfpkg import _mini_yaml
    """解析单个 skill.yaml；返回 (key, 骨架) 或 (None, 原因)。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, "读取失败：%s" % e
    try:
        try:
            import yaml
            man = yaml.safe_load(text) or {}
        except ImportError:
            man = _mini_yaml(text) or {}
    except Exception as e:
        return None, "解析失败：%s" % e
    if not isinstance(man, dict):
        return None, "顶层不是字典"
    try:
        schema = int(man.get("schema") or 1)
    except (TypeError, ValueError):
        return None, "schema 不是整数"
    if schema > SKILL_SCHEMA_MAX:
        return None, ("schema %d 高于本版 tf 支持的 %d，请升级 tf"
                      % (schema, SKILL_SCHEMA_MAX))
    sdir = os.path.dirname(os.path.realpath(path))
    key = str(man.get("name") or os.path.basename(sdir)).strip()
    if not key:
        return None, "缺少 name"
    if man.get("enabled") is False:
        return None, "__disabled__"
    skel = dict(man.get("defaults") or {})
    for k in _MANIFEST_TYPE_KEYS:
        if k in man and man[k] is not None:
            skel[k] = man[k]
    if not skel.get("steps"):
        return None, "没有 steps"
    skel["skill_dir"] = sdir               # 绝对路径，find_asset 直接可用
    skel.setdefault("desc", key)
    skel["_skill_manifest"] = path
    skel["_skill_version"] = man.get("version")
    skel["_skill_requires"] = man.get("requires") or {}
    chk = man.get("checks", "checks.py")
    cp = os.path.join(sdir, str(chk)) if chk else None
    # patch_common_opt：公共判据文件随 _common 重构挪进了公共步骤子目录（opt/ 等），
    # 但老 skill.yaml 仍写重构前路径（../_common/checks_relax.py）。字面路径找不到、
    # 且指向公共池时，按 basename 在 _common/*/ 里兜底一层——与 find_asset /
    # dim_common 加载里 <pool>/*/ 的处理保持一致，skill.yaml 一行都不用改。
    if cp and chk and not os.path.isfile(cp) and COMMON_POOL_DIR in str(chk):
        _pool = os.path.normpath(os.path.join(sdir, os.path.dirname(str(chk))))
        for _hit in sorted(glob.glob(os.path.join(_pool, "*",
                                                   os.path.basename(str(chk))))):
            if os.path.isfile(_hit):
                cp = _hit
                break
    skel["_skill_checks"] = cp if (cp and os.path.isfile(cp)) else None
    return key, skel

# ===== discover_skills (原 L1182-L1198) =====
def discover_skills(cfg, verbose=False):
    """扫描所有搜索路径，返回 {key: 骨架}；靠前路径优先，同名不覆盖。"""
    found, bad = {}, []
    for base in skill_search_dirs(cfg):
        for mp in sorted(glob.glob(os.path.join(base, "*", SKILL_MANIFEST))):
            key, skel = _load_manifest(mp)
            if key is None:
                if skel != "__disabled__":
                    bad.append((mp, skel))
                continue
            if key in found:
                continue
            found[key] = skel
    if bad and verbose:
        for mp, why in bad:
            sys.stderr.write("警告：技能清单 %s 已忽略（%s）\n" % (mp, why))
    return found

# ===== _merge_type (原 L1201-L1214) =====
def _merge_type(skel, over):
    """技能骨架 + 用户覆盖。标量/列表整体覆盖，一层字典递归合并；
    用户显式写 steps 就完全接管（保留手改逃生口）。"""
    out = dict(skel)
    for k, v in (over or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            m = dict(out[k])
            m.update(v)
            out[k] = m
        else:
            out[k] = v
    return out

# ===== apply_skills (原 L1217-L1235) =====
def apply_skills(cfg, verbose=False):
    """把发现到的技能并进 cfg['task_types']；已有同名段作为覆盖层。
    必须在 merge_project_configs 之前调用（项目段要叠在骨架之上）。"""
    skills = discover_skills(cfg, verbose=verbose)
    white = cfg.get("enabled_skills")
    black = set(cfg.get("disabled_skills") or [])
    tt = dict(cfg.get("task_types") or {})
    for key, skel in skills.items():
        if white and key not in white:
            continue
        if key in black:
            tt.pop(key, None)
            continue
        tt[key] = _merge_type(skel, tt.get(key) or {})
    for key in black:      # 黑名单也能关掉纯 tf.yaml 定义的类型
        tt.pop(key, None)
    cfg["task_types"] = tt
    cfg["_skills"] = skills
    return cfg

# ===== _seq_sort_steps (原 L1238-L1248) =====
def _seq_sort_steps(steps):
    """按 seq 稳定重排步骤（seq 解析不出来的保持原相对位置，不打乱老技能）。"""
    _keyed, _last = [], -1.0
    for _i, _s in enumerate(steps):
        _v = _seq_key(step_seq(_s))
        if _v is None:
            _v = _last + 1e-6 * (_i + 1)
        else:
            _last = _v
        _keyed.append((_v, _i, _s))
    return [_x[2] for _x in sorted(_keyed, key=lambda _x: (_x[0], _x[1]))]

# ===== expand_optional_steps (原 L1251-L1301) =====
def expand_optional_steps(t):
    """可选步骤组展开（取代写死的 PLOT_STEP_DEFS / _inject_plot_steps）。
    optional_steps.<开关名>.{default, steps[]}；步骤里的 after 是锚点名前缀，
    命中则插在最后一个匹配项之后，锚点不存在就不注入。
    先按名字剔除再插入，顺带修掉「段之间浅拷贝共享 steps 列表」的老问题。
    被关掉的组记到 t['_optional_off']（flag -> defs）与 t['_optional_off_flat']
    （name/label/seq -> (flag, def)），供 -j <步骤> start 按需启用。"""
    steps = t.get("steps")
    if not isinstance(steps, list):
        return
    steps = list(steps)
    off = {}
    for flag, spec in (t.get("optional_steps") or {}).items():
        spec = spec or {}
        defs = spec.get("steps") or []
        names = {d.get("name") for d in defs}
        steps = [s for s in steps if s.get("name") not in names]
        if t.get(flag, spec.get("default", True)) is False:
            off[flag] = [dict(d) for d in defs]
            continue
        for d in defs:
            anchor = d.get("after")
            d2 = {k: v for k, v in d.items() if k != "after"}
            pos = None
            if anchor:
                for i, s in enumerate(steps):
                    if str(s.get("name", "")).startswith(str(anchor)):
                        pos = i
                if pos is None:
                    continue
            steps.insert(pos + 1 if pos is not None else len(steps), d2)
    # patch_seq_order：各可选组按"声明顺序 + 插在锚点紧后面"注入，会互相挤位
    # （band 声明 plot_steps -> vacuum_align -> bandgap_hse，结果 seq 4 的
    # step4_HSE_band 锚在 step3_PBE_WAVECAR 上，把已插好的 3.1/3.2 挤到 4.x
    # 后面）。这既让表头乱序，也让 _dag_needs 的"上一步"回退连错依赖 ——
    # S3.1_plot 会挂在 step4_vacuum 后面，PBE 能带图白等整条 HSE 链。
    # 这里按 seq 稳定重排；seq 解析不出来的保持原相对位置，不打乱老技能。
    t["steps"] = _seq_sort_steps(steps)
    if off:
        t["_optional_off"] = off
        flat = {}
        for flag, defs in off.items():
            for d in defs:
                for k in ("name", "label", "seq"):
                    v = d.get(k)
                    if v is not None:
                        flat[str(v)] = (flag, d)
        t["_optional_off_flat"] = flat
    else:
        t.pop("_optional_off", None)
        t.pop("_optional_off_flat", None)

# ===== _seq_key (原 L1304-L1311) =====
def _seq_key(v):
    """把 seq / -j token 归一成可比较的数：'2'->2.0，'2.1'->2.1，非数->None。"""
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None

# ===== _name_seq (原 L1314-L1318) =====
def _name_seq(name):
    """从步骤名抽序号：step2 -> 2.0，step2.1_static -> 2.1，step1a_opt -> 1.0。
    抓 'step' 后面的数字（可带一位小数），字母后缀（a/b/c）忽略。"""
    m = re.match(r"step(\d+(?:\.\d+)?)", str(name or ""))
    return float(m.group(1)) if m else None

# ===== step_seq (原 L1321-L1326) =====
def step_seq(s):
    """步骤序号：优先清单里的 seq，缺省从 stepN 名字推。"""
    if s.get("seq") is not None:
        return str(s["seq"])
    m = re.match(r"step(\d+)", str(s.get("name", "")))
    return m.group(1) if m else None

# ===== skill_checks_for (原 L1329-L1343) =====
def skill_checks_for(cfg, keys):
    """收集这些技能的私有判据源码 {key: 源码}，随采集器 payload 下发。"""
    out = {}
    for k in keys:
        if k in out:
            continue
        p = ((cfg.get("_skills") or {}).get(k) or {}).get("_skill_checks")
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                out[k] = f.read()
        except OSError as e:
            sys.exit("错误：技能 %s 的判据文件 %s 读取失败（%s）。" % (k, p, e))
    return out

# ===== cmd_skills (原 L1346-L1368) =====
def cmd_skills(cfg, tt=None):
    """tf skills —— 列出已发现的技能。"""
    skills = cfg.get("_skills") or discover_skills(cfg, verbose=True)
    if not skills:
        print("没有发现任何技能清单（skill/*/skill.yaml）。搜索路径：")
        for d in skill_search_dirs(cfg):
            print("  " + d)
        return 0
    black = set(cfg.get("disabled_skills") or [])
    white = cfg.get("enabled_skills")
    print("%-10s %-8s %-6s %-6s %s" % ("技能", "版本", "步骤", "状态", "清单"))
    for k in sorted(skills):
        if tt and k != tt:
            continue
        s = skills[k]
        st = "关闭" if (k in black or (white and k not in white)) else "启用"
        print("%-10s %-8s %-6d %-6s %s"
              % (k, s.get("_skill_version") or "-", len(s.get("steps") or []),
                 st, s.get("_skill_manifest")))
    print("\n搜索路径（靠前优先）：")
    for d in skill_search_dirs(cfg):
        print("  " + d)
    return 0

# ===== 来自 03_projects.py =====
# -*- coding: utf-8 -*-
# 03_projects —— 项目配置扫描与合并 / 任务类型解析
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1371  scan_project_configs
#   L1415  _stepconf_param_from_file
#   L1434  resolve_stepconf_flags
#   L1445  merge_project_configs
#   L1492  _filter_run_steps
#   L1530  get_types
#   L1595  step_cfg

# ===== scan_project_configs (原 L1371-L1412) =====
def scan_project_configs(roots):
    """扫描项目根下 project_setting/tf_*.yaml。
    返回 [(配置名, 路径, 项目目录)]；配置名（tf_<名>.yaml 的 <名>）全局唯一，重复即报错。
    v-perf：单次 scandir 遍历（只下探目录、不枚举文件），深度≤6，跳过
    result/log/隐藏目录——比旧实现 7 个 glob（各遍历整棵树）更快，且不枚举
    数据文件（os.walk 在 WSL DrvFS 等慢盘上枚举文件极慢）。"""
    seen, found = {}, []
    for r in roots:
        r = os.path.realpath(os.path.expanduser(str(r)))
        if not os.path.isdir(r):
            print("警告：project_roots 里的 %s 不存在，跳过。" % r, file=sys.stderr)
            continue
        base_depth = r.rstrip(os.sep).count(os.sep)
        stack = [r]
        while stack:
            d = stack.pop()
            if d.count(os.sep) - base_depth > 6:
                continue
            try:
                it = os.scandir(d)
            except OSError:
                continue
            ps_entries, subdirs = [], []
            with it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if e.name == "project_setting":
                        ps_entries.append(e.path)
                    elif e.name not in ("result", "log") and not e.name.startswith("."):
                        subdirs.append(e.path)
            for ps in ps_entries:
                for p in sorted(glob.glob(os.path.join(ps, "tf_*.yaml"))):
                    name = os.path.basename(p)[len("tf_"):-len(".yaml")]
                    if name in seen and seen[name] != p:
                        sys.exit("错误：项目配置名重复 tf_%s.yaml：\n  %s\n  %s\n"
                                 "命名规则 tf_<项目名>.yaml，全局唯一，请改其中一个的名字。"
                                 % (name, seen[name], p))
                    seen[name] = p
                    found.append((name, p, os.path.dirname(os.path.dirname(p))))
            stack.extend(subdirs)
    return found

# ===== _stepconf_param_from_file (原 L1415-L1431) =====
def _stepconf_param_from_file(path, key):
    """极简读取 step.conf 里某 [params] 键的值（行尾 # / ! 注释剥掉）。
    找不到文件/键返回 None。只用于驱动装配阶段读 BANDGAP 这类步骤图开关，
    不替代 gen 脚本侧的 stepconf.py 完整合并。"""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                s = re.sub(r"\s+[#!].*$", "", ln).strip()
                if not s or s.startswith(("#", "!")):
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    if k.strip() == key:
                        return v.strip()
    except OSError:
        pass
    return None

# ===== resolve_stepconf_flags (原 L1434-L1442) =====
def resolve_stepconf_flags(seg, ps_dir):
    """把项目共用 step.conf 里的步骤图开关映射成可选步骤组的开/关，注入 seg。
    目前一条：BANDGAP = pbe|hse  ->  bandgap_hse（pbe 关掉整段 HSE）。
    仅在 seg 未显式写该开关时生效（项目配置里手写 bandgap_hse 优先级更高）。"""
    scp = os.path.join(ps_dir, "templates", "step.conf")
    if "bandgap_hse" not in seg:
        bg = _stepconf_param_from_file(scp, "BANDGAP")
        if bg is not None:
            seg["bandgap_hse"] = (bg.lower() != "pbe")

# ===== merge_project_configs (原 L1445-L1489) =====
def merge_project_configs(cfg):
    """把项目配置（project_setting/tf_*.yaml）的 task_types 合并进全局配置。
    同 key：项目配置作为该类型的一个"段"（独立 local_root，缺省字段继承全局/主定义）。
    local_root 缺省 = 该 project_setting 的父目录（项目自包含，可不写）。"""
    roots = cfg.get("project_roots") or []
    if not roots:
        roots = [t.get("local_root") for t in (cfg.get("task_types") or {}).values()
                 if isinstance(t, dict) and t.get("local_root")]
    found = scan_project_configs(roots)
    if not found:
        return cfg
    tt = cfg.setdefault("task_types", {})
    for name, path, proj_dir in found:
        try:
            pc = _load_yaml_file(path)
        except OSError as e:
            sys.exit("错误：项目配置 %s 读取失败：%s" % (path, e))
        for key, seg in (pc.get("task_types") or {}).items():
            seg = dict(seg or {})
            seg["_base_dir"] = os.path.dirname(path)
            lr = seg.get("local_root")
            if lr:
                lr = str(lr)
                if not os.path.isabs(lr):
                    lr = os.path.normpath(os.path.join(proj_dir, lr))
                seg["local_root"] = os.path.expanduser(lr)
            elif os.path.isfile(os.path.join(proj_dir, "POSCAR")):
                # 材料级 project_setting（qHPC20/project_setting）：
                # 缺省 local_root = 上一级（材料名 = 目录名，如 qHPC20）
                seg["local_root"] = os.path.dirname(proj_dir)
            elif os.path.isfile(os.path.join(os.path.dirname(proj_dir), "POSCAR")):
                # v1.7：技能子目录级 project_setting（Mg2C60/band/project_setting）：
                # proj_dir(=Mg2C60/band) 不是材料，其父(=Mg2C60)才是材料 →
                # local_root = 材料的父目录，材料才能被发现。
                seg["local_root"] = os.path.dirname(os.path.dirname(proj_dir))
            else:
                # 项目/体系级（C20/project_setting）：缺省 local_root = 父目录
                seg["local_root"] = proj_dir
            seg["_from"] = os.path.realpath(path)
            resolve_stepconf_flags(seg, os.path.dirname(path))  # step.conf 的 BANDGAP -> bandgap_hse
            if key in tt and tt[key] is not seg:
                tt[key].setdefault("_segments", []).append(seg)
            else:
                tt[key] = seg
    return cfg

# ===== _filter_run_steps (原 L1492-L1527) =====
def _filter_run_steps(t):
    """run_steps: 只保留指定步骤（子集，顺序不变）。
    元素写法：序号 1/2/3/4（第 N 个计算步骤；三段式弛豫时 1 = step1a/b/c
    三段全含，只跑某一段就写步骤名或 label，如 step1a_PBE_opt / S1a_ion）、
    3.1/4.1（画图步骤）。未配置 = 全部步骤。"""
    rs = t.get("run_steps")
    if not rs:
        return
    steps = t.get("steps") or []
    toks = rs if isinstance(rs, list) else [rs]

    def match(s, tok):
        ts = str(tok).strip()
        if ts == str(s.get("name", "")) or ts == str(s.get("label", "")):
            return True
        # v1.8：seq 优先，其次名字里的点号序号；点号精确比较，不做前缀近似
        want = _seq_key(ts)
        if want is None:
            return False
        sq = _seq_key(step_seq(s))
        if sq is None:
            sq = _name_seq(s.get("name"))
        return sq is not None and abs(sq - want) < 1e-9

    kept, kept_ids = [], set()
    for tok in toks:
        for s in steps:
            if match(s, tok) and id(s) not in kept_ids:
                kept.append(s)
                kept_ids.add(id(s))
    if not kept:
        sys.exit("错误：%s 的 run_steps 没有匹配到任何步骤（可用：%s）。"
                 % (t["key"], ", ".join(str(s.get("name")) for s in steps)))
    order = {id(s): i for i, s in enumerate(steps)}
    kept.sort(key=lambda s: order[id(s)])
    t["steps"] = kept

# ===== get_types (原 L1530-L1592) =====
def get_types(cfg, tt=None, root_override=None, quiet=False):
    """把配置归一化成类型列表；应用 -tt 过滤和 ROOT 覆盖。
    项目配置合并进来的同 key 段在此展开为多个类型实例（key 相同，local_root 不同）。"""
    raw = cfg.get("task_types")
    types = []
    if raw:
        for k, tc in raw.items():
            t = dict(tc or {})
            t["key"] = str(k)
            segs = t.pop("_segments", []) or []
            for s in segs:                       # 段 = 主定义 + 覆盖字段
                s2 = dict(t)
                s2.update({k2: v for k2, v in s.items() if v is not None})
                s2["key"] = str(k)
                types.append(s2)
            # 主定义自身无 local_root/root 时只作骨架（发现交给各段；
            # 暂时没有段也不报错——比如刚 init 一个新项目之前）
            if t.get("local_root") or t.get("root"):
                types.append(t)
    else:
        t = {k: cfg[k] for k in ("root", "steps", "gen_dir", "materials", "desc")
             if k in cfg}
        t["key"] = "-"
        types.append(t)
    # v1.1：all_keys 取全局定义的骨架全集——只定义了骨架、还没有项目挂段的
    # 类型（如新加的 elastic）此前不在 types 里，-tt 会误报"没有任务类型"
    all_keys = [str(k) for k in (raw or {}).keys()] or [t["key"] for t in types]
    for t in types:
        if not t.get("root"):
            t["root"] = cfg.get("root")
        t.setdefault("desc", t["key"])
    if tt:
        if tt not in all_keys:
            sys.exit("错误：没有任务类型 '%s'（已定义：%s）。"
                     % (tt, ", ".join(all_keys)))
        types = [t for t in types if t["key"] == tt]
        if not types and not quiet:   # init 时这条是废话（正要去生成），不打
            print("（%s：类型已在全局 tf.yaml 定义，但还没有项目挂这个类型。\n"
                  "  在材料的 project_setting/tf_<项目名>.yaml 里加一段  %s:  ，\n"
                  "  或用  tf -tt %s -p <材料> init  生成项目配置。）" % (tt, tt, tt))
    if root_override:
        if len(types) != 1:
            sys.exit("错误：配置了多个任务类型时，命令行指定 ROOT 必须同时用 -tt 指定类型。")
        types[0]["root"] = root_override
    for t in types:
        expand_optional_steps(t)   # v1.2：按技能清单展开可选步骤组
        _filter_run_steps(t)    # v1.0：run_steps 自定义步骤子集
    for t in types:
        if not t.get("root") and not t.get("local_root"):
            sys.exit("错误：任务类型 %s 没有 root/local_root"
                     "（在配置里写 local_root 或 root，或命令行指定）。" % t["key"])
        if t.get("local_root") and t.get("steps") is None:
            skels = [k for k, tc in (cfg.get("task_types") or {}).items()
                     if tc and tc.get("steps")]
            src = t.get("_from") or "project_setting/tf_*.yaml"
            sys.exit("错误：类型 %s 在全局 tf.yaml 里没有对应定义（继承不到 steps）。\n"
                     "来源文件：%s\n"
                     "通常是该文件的类型名和全局 tf.yaml 不一致（如 bd 改名 band 后没同步）。\n"
                     "全局可用的类型名：%s。修正命令：\n"
                     "  sed -i 's/^  %s:/  %s:/' %s"
                     % (t["key"], src, ", ".join(skels) or "（无）",
                        t["key"], skels[0] if skels else "band", src))
    return types

# ===== step_cfg (原 L1595-L1602) =====
def step_cfg(t, sname, m=None):
    """步骤配置；v3.1 起材料可携带所属段（_seg），段配置优先于类型条目。"""
    steps = ((m or {}).get("_seg") or {}).get("steps_cfg") \
        or t.get("steps_cfg") or t.get("steps") or []
    for s in steps:
        if s.get("name") == sname:
            return s
    return {}

# ===== 来自 04_discover.py =====
# -*- coding: utf-8 -*-
# 04_discover —— 本地材料与目录发现 / 解析
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1608  _natkey
#   L1612  discover_local
#   L1643  find_ps_dir
#   L1674  _load_yaml_file
#   L1681  load_project_settings
#   L1692  pkg_setting_path
#   L1706  resolve_material_local
#   L1801  log_action

# ===== _natkey (原 L1608-L1609) =====
def _natkey(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]

# ===== discover_local (原 L1612-L1637) =====
def discover_local(local_root):
    """本地项目根下发现有 POSCAR 的目录（root 自身 + ≤2 层嵌套；
    project_setting/result/log 天然无 POSCAR）。
    v1.1：root 自身也算——"一材料一项目"（local_root 直指材料目录，
    如 Ela1 挂 project_roots 根下）此前发现不到材料；与远端 discover
    的 cands=[root] 行为对齐。"""
    import glob as _glob
    root = os.path.realpath(os.path.expanduser(local_root))
    mats, picked = [], []
    # v1.3.1：已被识别为材料的目录，其内部不再嵌套发现材料——否则在材料
    # 目录里建个带 POSCAR 的 test/ 备份目录都会被当成新材料自动开算
    def _under_picked(d):
        rd = os.path.realpath(d)
        return any(rd == p or rd.startswith(p + os.sep) for p in picked)
    if os.path.isfile(os.path.join(root, "POSCAR")):
        mats.append({"name": os.path.basename(root), "lpath": root})
        picked.append(root)
    for pat in ("*", "*/*"):
        for d in sorted(_glob.glob(os.path.join(root, pat))):
            if (os.path.isdir(d)
                    and os.path.isfile(os.path.join(d, "POSCAR"))
                    and not _under_picked(d)):
                mats.append({"name": os.path.relpath(d, root), "lpath": d})
                picked.append(os.path.realpath(d))
    mats.sort(key=lambda m: _natkey(m["name"]))
    return root, mats

# ===== find_ps_dir (原 L1643-L1671) =====
def find_ps_dir(matdir, rootstop, subdir=None):
    """找最近的 project_setting/。优先级（v1.7 自包含布局）：
      1) <matdir>/<subdir>/project_setting —— 指定技能子目录时最优先
         （band/project_setting、elastic/project_setting 各自独立）；
      2) <matdir>/project_setting 起向上到 rootstop —— 老的就近向上
         （材料级 / 体系级共享配置，向后兼容）；
      3) 未指定 subdir（如孤儿检查，无技能上下文）时，兜底扫
         <matdir>/*/project_setting，任一技能子目录有即算已初始化。
    找不到返回 None。"""
    import glob as _glob
    d0 = os.path.realpath(matdir)
    rootstop = os.path.realpath(rootstop)
    if subdir:   # v1.7：技能级 project_setting 最优先
        cand = os.path.join(d0, str(subdir), "project_setting")
        if os.path.isdir(cand):
            return cand
    d = d0
    while True:
        cand = os.path.join(d, "project_setting")
        if os.path.isdir(cand):
            return cand
        if d == rootstop or not d.startswith(rootstop):
            break
        d = os.path.dirname(d)
    if not subdir:   # v1.7：无技能上下文时，任一技能子目录的 project_setting 都算数
        for h in sorted(_glob.glob(os.path.join(d0, "*", "project_setting"))):
            if os.path.isdir(h):
                return h
    return None

# ===== _load_yaml_file (原 L1674-L1678) =====
def _load_yaml_file(path):
    from tfpkg import _mini_yaml
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _mini_yaml(f.read()) or {}

# ===== load_project_settings (原 L1681-L1689) =====
def load_project_settings(ps_dir):
    """读取 project_setting 下的 setting.yaml 和 hpc.yaml（带缓存）。"""
    if ps_dir in _PS_CACHE:
        return _PS_CACHE[ps_dir]
    st = _load_yaml_file(os.path.join(ps_dir, "setting.yaml")) if ps_dir else {}
    hpc = _load_yaml_file(os.path.join(ps_dir, "hpc.yaml")) if ps_dir else {}
    ps = {"dir": ps_dir, "setting": st, "hpc": hpc}
    _PS_CACHE[ps_dir] = ps
    return ps

# ===== pkg_setting_path (原 L1692-L1700) =====
def pkg_setting_path(name):
    from tfpkg import _PKG_DIR, _PKG_ROOT
    """taskflow 包内 setting/<name>.yaml 的位置（兼容 versions/vX 与平铺布局）。"""
    for cand in (os.path.join(_PKG_ROOT, "setting", name),
                 os.path.join(_PKG_DIR, "setting", name),
                 os.path.expanduser("~/.config/taskflow/setting/" + name)):
        if os.path.isfile(cand):
            return cand
    return None

# ===== resolve_material_local (原 L1706-L1798) =====
def resolve_material_local(t, root, m):
    """给本地发现的材料补齐：project_setting、hpc、路径、远端目录、有效 host。"""
    # v1.1：skill_subdir 子目录名（band/elastic…）。v1.7：先算出来，find_ps_dir
    # 要用它定位技能级 project_setting（材料/<技能>/project_setting）。
    _subdir = (str(t.get("dir_name") or t["key"]) if t.get("skill_subdir") else None)
    ps = load_project_settings(find_ps_dir(m["lpath"], root, _subdir))
    st, hpc = ps["setting"], ps["hpc"]
    # 项目没有 hpc.yaml（或字段缺失）时回退到类型 hpc 配置：
    # v1.0 起 hpc 可写内联字典（把 setting/jzzn.yaml 的内容直接写进 tf.yaml），
    # 字符串仍是包内 setting/<hpc>.yaml 文件名
    thpc = t.get("hpc")
    if isinstance(thpc, dict):
        dhpc = dict(thpc)
    else:
        dhpc = {}
        dflt = pkg_setting_path(str(thpc or "jzzn") + ".yaml")
        if dflt:
            dhpc = _load_yaml_file(dflt)
    fmt = {"matdir": m["lpath"], "mat": m["name"], "root": root}

    def expand(v, default):
        v = v or default
        return v.format(**fmt) if isinstance(v, str) else v

    m["ps"] = ps
    # v1.1：skill_subdir——材料目录下按技能建子目录（如 elastic/、band/）。
    # 远端步骤目录 = work_dir/材料/<subdir>/stepN；本地 result = 材料/<subdir>/result。
    # 子目录名 dir_name 缺省 = 类型 key。老项目平铺结构不开此开关即可。
    m["_subdir"] = _subdir
    m["_skill_dir_local"] = (os.path.join(m["lpath"], m["_subdir"])
                             if m["_subdir"] else None)
    # v1.6：技能子目录里可放私有 hpc.yaml——同一材料的不同技能跑不同超算。
    # 字段级覆盖，优先级：材料/<技能>/hpc.yaml ＞ project_setting/hpc.yaml
    # ＞ 段级 hpc（内联 dict 或包内 setting/<名>.yaml）。
    if m["_skill_dir_local"]:
        shpc = _load_yaml_file(os.path.join(m["_skill_dir_local"], "hpc.yaml"))
        if shpc:
            merged = dict(hpc)
            merged.update(shpc)
            if hpc.get("template_map") or shpc.get("template_map"):
                tm = dict(hpc.get("template_map") or {})
                tm.update(shpc.get("template_map") or {})   # 映射级合并
                merged["template_map"] = tm
            hpc = merged
    # fix: hpc_name 统一转 str——项目 yaml 里 name: 3090 未加引号会被 YAML
    # 解析成 int，进而 render_table 的 len() 对 int 报 "has no len()"。
    m["hpc_name"] = str(hpc.get("name") or dhpc.get("name")
                        or (t.get("hpc") if isinstance(t.get("hpc"), str) else None)
                        or "jzzn")
    m["host_eff"] = hpc.get("ssh_host") or dhpc.get("ssh_host") or None
    # v1.11：work_dir 回退链——项目 setting.yaml > 项目 hpc.yaml >
    # 集群 setting/<hpc_name>.yaml 的 work_dir > 技能默认 > root。
    # 三个集群（jzzn/3090/a800）都在 setting/<name>.yaml 里自描述 work_dir。
    _cluster_work_dir = None
    _cp = pkg_setting_path(str(m["hpc_name"]) + ".yaml")
    if _cp:
        _cluster_work_dir = (_load_yaml_file(_cp) or {}).get("work_dir")
    m["work_dir_eff"] = (st.get("work_dir") or hpc.get("work_dir")
                         or _cluster_work_dir or t.get("work_dir")
                         or t.get("root"))
    # v1.11：用户没显式指定 work_dir 时提示（按技能去重，避免刷屏）
    if not (st.get("work_dir") or hpc.get("work_dir")):
        _wk = t.get("key")
        if _wk not in _WARN_WORKDIR:
            _WARN_WORKDIR.add(_wk)
            print("提示：技能 %s 未在项目里显式指定 work_dir，回退到 %s"
                  "（如需改，在 project_setting/setting.yaml 写 work_dir）"
                  % (_wk, m["work_dir_eff"] or "(无)"), file=sys.stderr)
    # v1.3：skill_subdir 开启时 result/log 默认进技能子目录；setting.yaml 里
    # 仍是 init 模板默认值（{matdir}/result）的视为"未定制"一并升级，
    # 定制过路径的尊重原值（elastic init 后不用再手改 setting）
    rd, ld = st.get("result_dir"), st.get("log_dir")
    if m["_subdir"]:
        if not rd or rd == "{matdir}/result":
            rd = "{matdir}/%s/result" % m["_subdir"]
        if not ld or ld == "{matdir}/log":
            ld = "{matdir}/%s/log" % m["_subdir"]
    m["result_dir"] = expand(rd, "{matdir}/%s/result" % m["_subdir"]
                             if m["_subdir"] else "{matdir}/result")
    m["log_dir"] = expand(ld, "{matdir}/%s/log" % m["_subdir"]
                          if m["_subdir"] else "{matdir}/log")
    # v1.11：fetch_files 三级回退——项目 setting.yaml > 技能 skill.yaml > VASP 默认。
    m["fetch_files"] = (st.get("fetch_files") or t.get("fetch_files") or [
        "INCAR", "POSCAR", "POTCAR", "KPOINTS", "KPOINTS_OPT", "kpath.json",
        "submit.sh", "OUTCAR", "CONTCAR", "EIGENVAL", "vasprun.xml", "queue.out"])
    tmap = dict(dhpc.get("template_map") or {})   # 映射级合并：包内默认补缺，
    tmap.update(hpc.get("template_map") or {})    # 项目 hpc.yaml 覆盖同名项
    m["template_map"] = tmap
    m["rpath"] = (os.path.join(m["work_dir_eff"], m["name"], m["_subdir"] or "")
                  if m["work_dir_eff"] else None)
    if m["rpath"]:
        m["rpath"] = os.path.normpath(m["rpath"])
    return m

# ===== log_action (原 L1801-L1813) =====
def log_action(m, text):
    """往该材料的 log_dir/tf.log 追加一行操作日志（本地模式才有）。"""
    ld = m.get("log_dir")
    if not ld:
        return
    try:
        os.makedirs(ld, exist_ok=True)
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(ld, "tf.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (ts, text))
    except OSError:
        pass

