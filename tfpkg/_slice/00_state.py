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
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tf.yaml"),
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "tf.yaml"),
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..",
                 "setting", "tf.yaml"),  # v1.0：也可放 setting/tf.yaml
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

