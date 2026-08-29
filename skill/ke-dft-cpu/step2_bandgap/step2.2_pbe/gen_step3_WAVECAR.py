#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step3_WAVECAR.py  (通用晶系 + KPOINTS_OPT 版)
=================================================
在父目录（含 step2_PBE_static/）下运行，生成能带所需 k 路径并搭建 step3_PBE_WAVECAR。

关键设计:
    1. 高对称路径【不再假设六方】。用 seekpath(HPKOT) 或 Setyawan-Curtarolo 约定
       自动识别布拉维格子并给出标准路径，任何晶系通用（三斜/单斜/菱方/……）。
       路径点由 seekpath 的 primitive 基底严格变换回【输入结构自己的】倒格子基底：
           L_prim = M @ L_in  (M 为整数矩阵)  =>  f_in = f_prim @ (M^-1)^T
       所以不需要把 POSCAR 换成标准原胞，step2 的弛豫构型原样保留。
       若输入是原胞的超胞（det M != ±1），会直接报错——那种情况能带会折叠，
       路径标签没有意义。
    2. 路径点写进 KPOINTS_OPT（VASP >= 6.3），不再塞进 KPOINTS 当零权重点。
       自洽只在 IBZKPT 的均匀加权点上做，HSE 阶段便宜一个数量级。
    3. 同时写出 kpath.json：标签、路径分段、每个 k 点的标签/断点索引。
       画图脚本直接读它，不再靠"分数坐标撞库"猜标签。

做的事:
    1. 读 step2 的 IBZKPT（均匀不可约网格 + 权重）与 CONTCAR/POSCAR（结构）。
    2. 自动求高对称路径（通用晶系），变换到输入基底。
    3. 写 step3_PBE_WAVECAR/{KPOINTS, KPOINTS_OPT, kpath.json}。
    4. 把 step2 的 CONTCAR/POSCAR、POTCAR 和改写后的 INCAR 写入 step3。
    5. 从 skill 目录的提交模板渲染 submit.sh（SOC->ncl / 无SOC->std，按维度选 2d/3d）。

INCAR 改写（step2 PBE 静态 -> step3 PBE 预收敛 WAVECAR）:
    删: LORBIT, NPAR, NCORE, KPAR, LSCALAPACK
    改/增: SYSTEM, ALGO=Normal, NELM=200, ISYM=2, LWAVE=.TRUE., LCHARG=.FALSE.,
           NBANDS, KPAR(自动)

用法:
    cd <父目录>      # 里面有 step2_PBE_static/（已跑完，有 IBZKPT）
    python gen_step3_WAVECAR.py

依赖: numpy, pymatgen；seekpath 可选（缺了自动退回 pymatgen 的 Setyawan-Curtarolo）

=====================================================================
★★★ submit.sh：模板渲染 + Slurm 参数覆盖（四个 step 脚本统一）★★★
=====================================================================
来源（与 step1/2/4 完全一致，互不依赖，可单独拆出来用）：
    从 skill 目录的提交模板渲染，按 SOC 与维度自动选：
        SOC=off -> submit_std_2d.tpl / submit_std_3d.tpl   (vasp_std)
        SOC=on  -> submit_ncl_2d.tpl / submit_ncl_3d.tpl   (vasp_ncl)
    逻辑名到实际文件（如 submit_jzzn_vaspstd_2d.tpl）的映射由 taskflow 的
    hpc.yaml/template_map 完成，本脚本只认逻辑名，换超算无需改本脚本。
    模板里只有 {{JOBNAME}} 一个占位符，其余 Slurm 参数写死在模板中。

覆盖（渲染之后再打补丁；值为 None 的项＝不改，保持模板原值）：
        SUBMIT_OVERRIDE = {
            "nodes":           None,   # #SBATCH --nodes=
            "ntasks_per_node": None,   # #SBATCH --ntasks-per-node=
            "qos":             None,   # #SBATCH --qos=
        }

关于本步的并行（★ 已修正旧版的错误说法）：
    KPOINTS_OPT 的硬性要求是 NCORE=1（等价于 NPAR=总进程数），所以本步
    【不写任何 NPAR/NCORE】，并把 step2 继承来的一并删掉。写了 NPAR=1
    这种"看起来无害"的值同样会被拒绝："I REFUSE TO CONTINUE WITH THIS SICK JOB"
    —— 因为 NPAR=1 恰恰等于 NCORE=ranks，正好是它禁止的那一种。

    旧版注释说 "KPAR>1 也会被拒绝"，这是错的。KPAR 约束的是 k 点层面的
    进程分组，与"组内不许切分轨道"的检查互不冲突；KPOINTS_OPT_NKBATCH
    这个标签本来就是配合 k 点并行用的。本步现在由 auto_parallel() 自动
    选一个合法 KPAR（见 KPAR_MAX），并把 NBANDS 对齐到组内 NPAR 的倍数。

    因此 ntasks_per_node 不再需要压到 24：有了 KPAR，组内核数 = 总核数/KPAR，
    不会出现"几十条能带摊在 96 个核上"的病态分解。若要开满节点，
    把下面的 ntasks_per_node 调大即可，脚本会自动重新挑 KPAR。
=====================================================================
"""

import os
import re
import shutil
import subprocess  # [vaspkit-auto]
import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import require_dim, filter_kpath_2d, resolve_dim, resolve_tpl  # noqa: E402
import stepconf  # noqa: E402

# ============================== 配置 ==============================

# ###################################################################
# ★ k 路径配置 —— 最常调的都在这儿，改完直接跑，不用往下翻 ★
# ###################################################################
#
# 常见场景速查
# ┌──────────────────────────────┬──────────────────────────────────┐
# │ 我想……                       │ 就改这个                          │
# ├──────────────────────────────┼──────────────────────────────────┤
# │ 2D，用脚本内置路径（默认）    │ 什么都不用改                      │
# │ 2D/3D，改用 vaspkit 的路径    │ KPATH_METHOD = "vaspkit"         │
# │ 自己指定路径                  │ KPATH_METHOD = "manual"          │
# │                              │ + 填 MANUAL_KPOINTS/SEGMENTS      │
# │ 能带线上的点太稀/太密         │ LINE_DENSITY                      │
# │ 没识别出该有的对称性          │ SYMPREC 放宽到 5e-2               │
# │ vaspkit 不在 PATH 里          │ VASPKIT_EXE 写绝对路径            │
# └──────────────────────────────┴──────────────────────────────────┘
#
# 投任务前先看一眼路径对不对（只打印，不生成任何文件）：
#     python gen_step3_WAVECAR.py --preview-kpath
#     python gen_step3_WAVECAR.py --preview-kpath POSCAR step1_PBE_opt/CONTCAR
#                                  ↑ 给两个结构 = 检查弛豫有没有改变格子类型


# ================= ① 用哪套路径 =================
# "seekpath"           3D 默认。Hinuma(HPKOT) 约定，覆盖所有布拉维格子。
#                      需 pip install seekpath；没装会自动退回 setyawan_curtarolo。
#                      ★ dim=2D 时会被 AUTO_2D_KPATH 自动改成 native_2d（见 ②）
# "setyawan_curtarolo" 纯 pymatgen，不需要 seekpath
# "native_2d"          【2D 专用】本文件内置的二维布拉维格子分类，单段闭合路径：
#                          正方 Γ-X-M-Γ   六方 Γ-M-K-Γ   矩形/有心矩形/斜方 Γ-X-S-Y-Γ
#                      不存在 `X|Γ` 跳变，标签与文献一致。一般不用手写，
#                      dim=2D 时由 AUTO_2D_KPATH 自动启用。
# "vaspkit"            用 vaspkit 的路径（2D=task 302，体相=task 303）。
#                      RUN_VASPKIT_KPATH=True 时脚本自己调 vaspkit，不用你进菜单。
#                      想和用 vaspkit 约定的文献严格对齐时选它。
# "manual"             用下面的 MANUAL_KPOINTS / MANUAL_SEGMENTS 手写
KPATH_METHOD = "seekpath"


# ================= ② 2D 内置路径 =================
# True  : dim=2D 且 KPATH_METHOD 是 seekpath/setyawan_curtarolo 时，自动改用
#         native_2d。因为 seekpath 按【三维】布拉维格子给路径，它不知道 c 是真空，
#         先按 3D 算完再剔面外点会把连续路径挖断成 `Γ-X | Γ-Z | U-Γ` 这种碎片，
#         标签也沿用 3D 约定，没法和文献对照。
#         KPATH_METHOD 写成 "vaspkit"/"manual" 时本开关不生效（尊重手动指定）。
# False : 退回旧行为（3D 路径 + 下面的 FILTER_KPATH_2D 剔面外点）。
AUTO_2D_KPATH = True

# 2D 且【没走】native_2d 时，从 3D 路径里剔除真空方向分量非零的点（A/L/H 之类）。
# 走了 native_2d 就没有面外点可剔，本开关自动失效。
# 维度本身继承自 step2 的 workflow_method.txt（DIM=），缺失时按结构现场判定。
FILTER_KPATH_2D = True


# ###################################################################
# ★ 展宽 / 对称性 / 并行 —— 这三项 step3 会覆盖模板，配置都在这儿 ★
# ###################################################################

# ================= 展宽 ISMEAR / SIGMA =================
# 模板里为半导体设的 ISMEAR=0/SIGMA=0.05，对金属体系是错的（能量和占据数都不准）。
# 但 step3 又绝不能用 ISMEAR=-5：本步 KPOINTS 是显式加权点列表 / 含零权重路径点，
# 没有 Tetrahedra 块，VASP 读 KPOINTS 时会直接
# "Error reading KPOINTS file ... I REFUSE TO CONTINUE" 罢工。
#
# "auto"          读 step2 的 EIGENVAL 判断金属/半导体，再选展宽（推荐）
#                   有能带跨越费米面 -> 金属 -> ISMEAR=1 / SIGMA=SIGMA_METAL
#                   否则           -> 半导体 -> ISMEAR=0 / SIGMA=SIGMA_SEMI
# "semiconductor" 强制 ISMEAR=0 / SIGMA_SEMI
# "metal"         强制 ISMEAR=1 / SIGMA_METAL
# "inherit"       原样继承 step2 的 ISMEAR/SIGMA（若是 -5 会被强制改成 0 并告警）
SMEARING_MODE = "auto"
SIGMA_SEMI    = "0.05"
SIGMA_METAL   = "0.20"
METAL_GAP_TOL = 0.05      # eV；判定为金属的带隙阈值

# ================= 对称性 ISYM =================
# "auto" -> 2。为什么不是 0（VASP Wiki "Why is symmetrization necessary"）:
#   自洽用的是对称约化后的不可约 k 点集，这本身就破坏了电荷密度的对称性，
#   必须靠对称化把它恢复回来。ISYM<=0 时这一步压根不做，rho 会带着人为的
#   对称破缺 —— 不是"慢一点"，是算错。
#   规矩：关对称性 -> 必须给完整网格；给不可约网格 -> 必须开对称性。
#
# 什么时候该改成 0：加外电场(EFIELD)、偶极修正(LDIPOL+IDIPOL)、Berry 相极化
#   (LCALCPOL)，以及非共线 SOC 想稳妥起见时。
#   ★ 本脚本默认 SCF_MESH_SOURCE="kpoints"（沿用 step2 的自动网格格式），
#     VASP 会用【本步自己的 ISYM】重新约化，所以改成 0 是安全的 —— VASP 自己
#     会生成与 ISYM=0 相符的完整网格。只有 SCF_MESH_SOURCE="ibzkpt"（显式列表）
#     时 ISYM 必须 >=1，脚本会在那种组合下直接报错拦住你。
# 这个值同时会被 step4 继承为它的默认 ISYM（HSE 那步会 +1 变成 3，见 step4 注释）。
STEP3_ISYM = "auto"       # "auto" | "0" | "1" | "2" | "3"

# ================= 并行 NCORE / KPAR =================
# "auto"    按提交脚本里的总核数自动算 KPAR 并注入（原行为）
# "inherit" 原样继承模板/step2 的 KPAR，不自动算
# 两种模式下 NCORE/NPAR 都会被删除：KPOINTS_OPT 的驱动要求 NCORE=1，
# 写了任何 NPAR/NCORE>1 会被 VASP 拒绝 "I REFUSE TO CONTINUE WITH THIS SICK JOB"。
# 这一条没有开关 —— 它不是偏好问题，是硬约束。
PARALLEL_MODE = "auto"    # "auto" | "inherit"
# ###################################################################


# ================= ③ vaspkit =================
# 仅在 KPATH_METHOD = "vaspkit" 时生效。
#
# RUN_VASPKIT_KPATH
#   True  : 脚本自己跑 vaspkit —— 把 step2 的 CONTCAR 拷进 VASPKIT_KPATH_DIR 当
#           POSCAR，调 `3 -> 302`（2D）或 `3 -> 303`（体相），再读回 KPATH.in。
#           那个目录不删，KPATH.in / PRIMCELL.vasp 留着供你核对。
#   False : 读 VASPKIT_KPATH_FILE 指向的现成文件（需你先手工跑一次 vaspkit）。
RUN_VASPKIT_KPATH  = True
VASPKIT_EXE        = "vaspkit"        # 不在 PATH 里就写绝对路径
VASPKIT_KPATH_TASK = "auto"           # "auto" = 2D 用 302、体相用 303；也可写死 "302"/"303"
VASPKIT_KPATH_DIR  = ".vaspkit_kpath" # 工作目录，跑完不删，方便核对
VASPKIT_KPATH_FILE = "KPATH.in"       # RUN_VASPKIT_KPATH=False 时从这里读

# vaspkit 的 KPATH.in 坐标是相对它标准化出的 PRIMCELL.vasp 写的，
# 若那个原胞和你的结构不是一回事，直接拿来用会画出一条错的能带。
#   True  : PRIMCELL 与你的结构若是【同一布拉维格子的不同基矢选择】
#           （变换矩阵为整数且 |det|=1），自动把坐标换算到你自己 POSCAR 的基底，
#           不必换结构重跑。体积都不同（如你的是超胞）仍然报错。
#   False : 只要 PRIMCELL 与结构不完全相同就报错（旧行为）。
VASPKIT_REBASE = True


# ================= ④ 手写路径 =================
# 仅在 KPATH_METHOD = "manual" 时生效。
# 分数坐标以【输入 POSCAR 的倒格子】为基；真空在 c 时第三个分量恒为 0。
# 2D 常用（照抄即可，注意换成你自己的格子类型）：
#   六方   MANUAL_KPOINTS  = {"G": (0,0,0), "M": (0.5,0,0), "K": (1/3,1/3,0)}
#          MANUAL_SEGMENTS = [["G","M","K","G"]]
#   正方   MANUAL_KPOINTS  = {"G": (0,0,0), "X": (0.5,0,0), "M": (0.5,0.5,0)}
#          MANUAL_SEGMENTS = [["G","X","M","G"]]
#   矩形   MANUAL_KPOINTS  = {"G": (0,0,0), "X": (0.5,0,0),
#                             "S": (0.5,0.5,0), "Y": (0,0.5,0)}
#          MANUAL_SEGMENTS = [["G","X","S","Y","G"]]
# 段与段之间是跳变（画出来会有竖线），所以尽量只写一段、首尾都是 G。
MANUAL_KPOINTS  = {}
MANUAL_SEGMENTS = []


# ================= ⑤ 密度与容差 =================
LINE_DENSITY = 40     # 路径点密度（点数 /（2π/Ang））。40 对 HSE 偏密，嫌慢可降到 20~30
EXTRA_KPTS   = []     # 追加额外路径点：[("label",(kx,ky,kz)), ...]（输入 POSCAR 基底）
SYMPREC   = 1e-2      # 对称性识别容差（Ang）。找不到该有的对称性时放宽到 5e-2
ANGLE_TOL = 5.0       # 对称性识别角度容差（度）

# 注：路径点是塞进 KPOINTS_OPT 还是走零权重旧方案，由下方的 USE_KPOINTS_OPT 决定
#     （VASP < 6.3 必须设成 False）。那个开关和 VASP 版本绑定，没有挪上来。
# ###################################################################

STEP2_DIR    = "step2_bandgap/step2.1_static"     # 源目录
STEP3_DIR    = "step2_bandgap/step2.2_pbe"    # 目标目录

STRUCT_FILE  = None       # None=自动找 CONTCAR 再 POSCAR
IBZKPT_FILE  = "IBZKPT"
INCAR_FILE   = "INCAR"
POTCAR_FILE  = "POTCAR"
OUTCAR_FILE  = "OUTCAR"
METHOD_FILE  = "workflow_method.txt"

# ---- submit.sh：从 skill 模板渲染（不继承 step2），再按 SUBMIT_OVERRIDE 覆盖 ----
# SOC 开 -> submit_ncl（vasp_ncl）；SOC 关 -> submit_std（vasp_std）。
# 实际模板按维度取 *_2d.tpl / *_3d.tpl（resolve_tpl 会回退到无后缀旧名）。
SUBMIT_TPL_NCL = "submit_ncl"
SUBMIT_TPL_STD = "submit_std"
SUBMIT_DEFAULTS = {
    "JOBNAME": None,      # None = 自动生成 <label>_s3wave
}
# 渲染后覆盖这三个 Slurm 参数；None = 不改，保持模板原值。
# ntasks_per_node 主机自适应：3090 GPU（1 rank 绑 1 卡）→ 1；jzzn CPU → 24。
import os as _os
_NTASKS_DEFAULT = (1 if _os.path.isdir("/home/wangchaoyue852")
                   else 24)
SUBMIT_OVERRIDE = {
    "nodes":           None,
    # 有了 KPAR 之后这里不再是"必须压到 24"；保留 24 作为保守默认值。
    # 想跑满节点就改成 48/96，auto_parallel() 会自动重挑合法 KPAR 与 NBANDS。
    "ntasks_per_node": _NTASKS_DEFAULT,
    "qos":             None,
}

# ---- k 路径相关配置已统一挪到文件开头的「★ k 路径配置」区块 ----
#      （FILTER_KPATH_2D / AUTO_2D_KPATH 在那里）

SUPPORTED_FUNCS = ("pbe-d3", "pbesol", "pbe")

# NBANDS: "auto"=自动确定；也可直接写整数强制指定
STEP3_NBANDS = "auto"
NBANDS_ROUND = 8          # 估算时向上取整到该整数的倍数

# 自旋轨道耦合。
# "auto": POSCAR 含 SOC_ELEMS（Z>=50 的重元素，In/Sn/Sb/Te/…/Pb/Bi）任意一种就开。
# True / False: 强制开 / 强制关。
# 打开后本步会:
#   - 自动把 NBANDS 翻倍（非共线每条自旋子带只装 1 个电子）
#   - 注入 LSORBIT=.TRUE./GGA_COMPAT=.FALSE./LMAXMIX=4，并按磁性配置写 MAGMOM(3*NIONS 分量)
#     （磁性由下方"磁性体系"一节控制；非磁时写 3*NIONS 个 0，与旧行为一致）
#   - 强制 NCORE=1（vasp_ncl 在 NCORE>1 时会于初始化阶段触发 FPE）
#   - submit.sh 用 submit_ncl.tpl（vasp_ncl），产出【非共线 WAVECAR】供 step4 热启动
# 关闭时: submit.sh 用 submit_std.tpl（vasp_std），保留 INCAR_SET 里的 NCORE。
SOC = "auto"            # "auto" | True | False
SOC_ELEMS = {           # Z >= 50
    "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba",
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er",
    "Tm", "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn", "Fr", "Ra", "Ac", "Th", "Pa", "U",
    "Np", "Pu", "Am", "Cm",
}

# INCAR 改写规则
# ★ NPAR 必须剔除：NPAR 与 NCORE 同时出现时 VASP 取 NPAR，会悄悄推翻 SOC 分支
#   强制的 NCORE=1（vasp_ncl 等效 NCORE>1 时初始化阶段 FPE），也会破坏 KPOINTS_OPT
#   要求的 NPAR=ranks。并行只用 NCORE 表达——incar.tpl/step2 里若写了 NPAR，到此为止。
INCAR_REMOVE = ["LORBIT", "NPAR", "NCORE", "KPAR", "LSCALAPACK"]
INCAR_SET = {
    "SYSTEM": "PBE pre-converge WAVECAR for HSE band-dft-cpu (step3)",
    "ALGO":   "Normal",
    "NELM":   "200",
    # ★★★ ISYM = 2（PAW 默认值），不再用 0 ★★★
    #   为什么原来的 ISYM=0 是错的（VASP Wiki "Why is symmetrization necessary"）:
    #     自洽用的是【对称约化后的不可约 k 点集】，这本身就破坏了电荷密度的对称性；
    #     必须靠对称化操作把密度恢复回来。ISYM<=0 时这一步压根不做，
    #     于是 rho 带着人为的对称破缺 —— 不是"慢一点"，是算错。
    #   到 step4 更严重: HSE 的 Fock 交换要在整个 BZ 上对 q 求和，
    #     ISYM=3 正是靠把不可约区的轨道旋转出来构造全 BZ；ISYM=0 时无操作可用。
    #   规矩: 关对称性 -> 必须给完整网格；给不可约网格 -> 必须开对称性。
    #   SOC 用户若坚持 ISYM=0，请把 SCF_MESH_SOURCE 设为 "kpoints"（自动网格），
    #     这样 VASP 会自己生成与 ISYM=0 相符的完整网格，并把 step4 的 ISYM 也设为 0。
    # ISYM / ISMEAR / SIGMA 由主流程按上面的 STEP3_ISYM / SMEARING_MODE 注入
    "LWAVE":  ".TRUE.",
    "LCHARG": ".FALSE.",
    "LASPH":  ".TRUE.",
    # ★ 不写 NPAR / NCORE：KPOINTS_OPT 驱动要求 NCORE=1（等价 NPAR=ranks），
    #   写了任何 NPAR/NCORE>1 都会被 VASP 拒绝："I REFUSE TO CONTINUE WITH THIS SICK JOB"。
    #   并行加速改用 KPAR（k 点级并行），由 auto_parallel() 自动注入。
}

# vasp_ncl 下强制使用的 NCORE（见上方说明）
SOC_NCORE = "1"

# ---- 并行：KPOINTS_OPT 下唯一可用的加速旋钮是 KPAR ----
# 之前的注释说 "KPAR>1 会被 VASP 拒绝"，那是错的。VASP 拒绝的是 NCORE>1
# （即 NPAR != ranks）；KPAR 把进程切成若干独立的 k 点组，与该检查不冲突，
# 而 KPOINTS_OPT_NKBATCH 这个参数本来就是配合 k 点并行用的。
# 约束（auto_parallel 会自动满足）：
#   1) 总核数 % KPAR == 0
#   2) KPAR <= 自洽网格的不可约 k 点数（否则有组闲着）
#   3) NBANDS % (总核数 / KPAR) == 0（否则 VASP 会偷偷抬高 NBANDS）
KPAR_MAX = 8              # KPAR 上限；内存紧张就调小（每个 k 组各存一份密度/波函数）

# ---- 自洽网格 KPOINTS 的来源 ----
# "kpoints"（推荐）: 直接沿用 step2 的自动网格 KPOINTS 文件，让 VASP 在本步
#                    用【本步自己的 ISYM / SOC 设置】重新做对称约化。
#                    自洽、闭环，ISYM 想设几都不会不一致。
# "ibzkpt"（旧行为）: 把 step2 的 IBZKPT 展开成显式加权列表写进 KPOINTS。
#                    这份列表是 step2 在 ISYM=2 下约化出来的，所以本步的 ISYM
#                    必须同样 >=1 才自洽；SOC 打开时对称性更低，更容易出偏差。
# step3 不读 step2 的 WAVECAR（step2 是 LWAVE=.FALSE.），所以两步的 k 点集
# 不需要一一对应，换成自动网格是安全的。
SCF_MESH_SOURCE = "kpoints"     # "kpoints" | "ibzkpt"

# ============================== 磁性体系 ==============================
#   MAGNETIC="auto" 判定顺序:
#     1) MAGMOM_PER_SPECIES 非空                          -> 磁性(手动磁矩)
#     2) step2 是 ISPIN=2 且 OUTCAR 有收敛磁矩:
#          max|m| >  MAG_ZERO_TOL                         -> 磁性(继承收敛磁矩)
#          max|m| <= MAG_ZERO_TOL                         -> 已塌缩，自动按非磁
#     3) step2 是 ISPIN=2 但读不到磁矩                    -> 磁性(需手动给磁矩，否则告警回退)
#     4) step2 非磁但 POSCAR 含 MAG_ELEM_MOMENTS 里的元素 -> 磁性(元素高自旋起点)+告警自愈
#     5) 其余                                             -> 非磁
#   也可硬写 True/False。
#   初始磁矩来源优先级: MAGMOM_PER_SPECIES(手动) > step2 OUTCAR 的收敛磁矩 > 元素默认表。
#     ★ 想要 AFM 等特定磁序，最稳的是让 step1/step2 就以该磁序收敛，step3 直接继承
#       OUTCAR 里每个离子的收敛磁矩（能保号、保 AFM）。per-species 只能给同号(FM 型)起点。
#   SOC(非共线)时: 每离子写 3 分量、磁矩沿 SAXIS(默认 z)，并【剔除 ISPIN】(非共线不用它，
#                  step4 也拒收 ISPIN=2)。
#   LDA+U(LDAU/LDAUL/LDAUU/…)从 step2 原样继承，无需在此重复；d+U 需 LMAXMIX≥4(SOC 分支已置 4)。
MAGNETIC = "auto"           # "auto" | True | False
MAGMOM_PER_SPECIES = {}     # 例(高自旋 Mn²⁺): {"Mn": 5.0, "In": 0.0, "Se": 0.0}
MAG_ELEM_MOMENTS = {        # 元素默认高自旋起点（step1/step2 同款表；仅作最后兜底）
    "Sc": 1.0, "Ti": 1.0, "V": 3.0, "Cr": 4.0, "Mn": 5.0,
    "Fe": 4.0, "Co": 3.0, "Ni": 2.0, "Cu": 1.0,
    "Ce": 1.0, "Pr": 2.0, "Nd": 3.0, "Pm": 4.0, "Sm": 5.0, "Eu": 7.0,
    "Gd": 7.0, "Tb": 6.0, "Dy": 5.0, "Ho": 4.0, "Er": 3.0, "Tm": 2.0, "Yb": 1.0,
    "U": 2.0, "Np": 3.0, "Pu": 4.0,
}
SAXIS = (0, 0, 1)           # 非共线自旋量子化轴；改它会改变 MAGMOM 的坐标系解释
MAG_ZERO_TOL = 0.1          # |磁矩| 低于此视作 0（用于 auto 判定与塌缩告警）

# ---- k 路径相关配置已统一挪到文件开头的「★ k 路径配置」区块 ----
#      （KPATH_METHOD / VASPKIT_* / MANUAL_* / LINE_DENSITY / SYMPREC 都在那里）

# ---- 关键开关：路径点放 KPOINTS_OPT 还是塞进 KPOINTS（零权重旧方案）----
# True  (推荐, 需 VASP >= 6.3):
#     KPOINTS     = 只有均匀加权点（IBZKPT 那 N 个）
#     KPOINTS_OPT = 全部路径点
#     VASP 先只用均匀网格做自洽，收敛后再对 KPOINTS_OPT 做一次 one-shot 对角化。
#     -> HSE 自洽阶段的 k 点数从 (N_uni + N_path) 降到 N_uni；
#        step3 的 WAVECAR 也只含 N_uni 个 k 点，step4 主 KPOINTS 与之一一对应。
#     -> 本征值写到 vasprun.xml 的 <eigenvalues_kpoints_opt>（VASP 不产出
#     EIGENVAL_OPT！EIGENVAL 里只有自洽网格点），画图脚本已同步适配。
# False (旧方案, 任何版本可用):
#     KPOINTS = 均匀加权点 + 零权重路径点，路径点在每个电子步都被重算。
USE_KPOINTS_OPT = True

# KPOINTS_OPT 的 one-shot 阶段一次同时处理多少个 k 点（越小越省内存、越慢）。
# None = 不写该标签（用 VASP 默认）。SOC + 大体系内存吃紧时设 8~24。
KPOINTS_OPT_NKBATCH = 24
# =================================================================


def read_poscar(path):
    with open(path) as f:
        lines = f.readlines()
    scale = float(lines[1].split()[0])
    vecs  = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)])
    if scale < 0:
        vol   = abs(np.linalg.det(vecs))
        scale = (abs(scale) / vol) ** (1.0 / 3.0)
    return vecs * scale


# ---------------------------------------------------------------------------
# 通用高对称路径：自动识别布拉维格子 -> 变换回输入结构的倒格子基底
# ---------------------------------------------------------------------------
TWOPI = 2.0 * np.pi


def _pretty(label):
    """seekpath/pymatgen 标签 -> 可显示标签。GAMMA->Γ, H_2->H₂"""
    if label in ("GAMMA", "G", "\\Gamma"):
        return "Γ"
    m = re.match(r"^([A-Za-z]+)_?(\d+)$", label)
    if m:
        sub = str(m.group(2)).translate(str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉"))
        return m.group(1) + sub
    return label


def _to_input_basis(struct, labels_prim, lat_prim):
    """
    把 primitive 基底下的分数坐标变到输入结构的基底。
        L_prim = M @ L_in        (M 整数, 行向量约定)
        B_prim = (M^-1)^T @ B_in
        f_in   = f_prim @ (M^-1)^T
    返回 (labels_in, detM)。输入若是原胞的超胞会抛错（能带会折叠）。
    """
    from pymatgen.core import Lattice
    v_in, v_pr = struct.lattice.volume, lat_prim.volume
    ratio = v_in / v_pr
    if ratio > 1.05:
        raise ValueError(
            "输入结构是原胞的 %.2f 倍超胞（V_in=%.2f, V_prim=%.2f Ang³）。"
            "超胞上的能带是折叠的，高对称点标签没有物理意义。"
            "请先把 step1/step2 换成原胞再跑。" % (ratio, v_in, v_pr))

    mapping = None
    for ltol, atol in ((1e-4, 0.3), (1e-3, 1.0), (1e-2, 3.0)):
        mapping = struct.lattice.find_mapping(lat_prim, ltol=ltol, atol=atol)
        if mapping is not None:
            break
    if mapping is None:
        raise ValueError("无法把 primitive 格子映射回输入格子——"
                         "试着放宽 SYMPREC，或改用 KPATH_METHOD='manual'")
    _aligned, _rot, M = mapping
    M = np.asarray(M, dtype=float)
    dev = float(np.abs(M - np.round(M)).max())
    if dev > 1e-3:
        raise ValueError("primitive<-输入 的变换矩阵不是整数矩阵（偏差 %.1e）" % dev)
    M = np.round(M)
    detM = float(np.linalg.det(M))
    if abs(abs(detM) - 1.0) > 1e-6:
        raise ValueError("|det M| = %.3f != 1，输入不是原胞（能带会折叠）" % abs(detM))

    MinvT = np.linalg.inv(M).T
    labels_in = {lab: tuple(np.asarray(f, dtype=float) @ MinvT)
                 for lab, f in labels_prim.items()}
    return labels_in, detM


def run_vaspkit_kpath(exe, struct_path, dim, workdir=None):  # [vaspkit-auto]
    """调 vaspkit 生成 KPATH.in，返回它的路径。

    vaspkit 只认当前目录下的 POSCAR，所以先把结构拷进一个专用目录再跑。
    该目录不删除：KPATH.in / PRIMCELL.vasp 留着供你核对。
    """
    task = VASPKIT_KPATH_TASK
    if task == "auto":
        task = "302" if dim == "2d" else "303"
    wd = Path(workdir or VASPKIT_KPATH_DIR)
    wd.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(struct_path, wd / "POSCAR")
    for f in ("KPATH.in", "PRIMCELL.vasp"):
        if (wd / f).exists():
            (wd / f).unlink()            # 清掉上一次的，避免读到陈旧结果

    print("[..] 调用 vaspkit 生成 k 路径：3 -> %s（%s，工作目录 %s）"
          % (task, "2D" if task == "302" else "体相", wd), file=sys.stderr)
    try:
        r = subprocess.run([exe], input="3\n%s\n0\n" % task, text=True,
                           cwd=str(wd), capture_output=True, timeout=300)
    except FileNotFoundError:
        raise ValueError("找不到 vaspkit 可执行文件 %r —— 改 VASPKIT_EXE，"
                         "或把 RUN_VASPKIT_KPATH 设为 False 手工生成 KPATH.in" % exe)
    except subprocess.TimeoutExpired:
        raise ValueError("vaspkit 超过 300 s 没退出，可能卡在交互菜单上。"
                         "请手工跑一次 `vaspkit -task %s` 看它问了什么。" % task)

    kf = wd / "KPATH.in"
    if not kf.exists():
        tail = (r.stdout or "")[-800:]
        raise ValueError("vaspkit 没有产出 KPATH.in（returncode=%d）。\n"
                         "        vaspkit 输出末尾：\n%s" % (r.returncode, tail))
    print("[OK] KPATH.in 已生成：%s" % kf, file=sys.stderr)
    return str(kf)


def kpath_from_vaspkit(path, struct_path):
    """解析 vaspkit 的 KPATH.in（task 301/302/303 的产物，Line-Mode 格式）。

    返回值与 auto_kpath 完全同构，所以下游（build_path / kpath.json / 2D 过滤）
    一行都不用改。

    ★ 唯一但致命的前提：KPATH.in 里的分数坐标是相对 vaspkit 标准化出来的
      【原胞】倒格子基底的。若你的 POSCAR 不是那个原胞，坐标就是错的。
      vaspkit 会同时吐出 PRIMCELL.vasp；本函数在它存在时自动比对晶格矢量，
      对不上就直接报错，不给你留下"路径悄悄错了"的机会。
    """
    p = Path(path)
    if not p.exists():
        raise ValueError("找不到 %s —— 请先运行 `vaspkit -task 302`（2D）或 "
                         "`vaspkit -task 303`（体相）生成 KPATH.in" % p)
    L = [ln.rstrip() for ln in p.read_text(encoding="utf-8-sig",
                                           errors="ignore").splitlines()]
    if len(L) < 6:
        raise ValueError("%s 内容太短，不像 KPATH.in" % p)
    if L[2].strip()[:1].lower() != "l":
        raise ValueError("%s 第 3 行不是 Line-Mode（读到 %r）" % (p, L[2].strip()))
    if L[3].strip()[:1].lower() not in ("r", "d"):
        raise ValueError("%s 用的是笛卡尔坐标，本脚本只接受 Reciprocal/Direct" % p)

    # ---- 晶格一致性检查：POSCAR vs PRIMCELL.vasp ----
    _rebase = None                                        # [vaspkit-auto]
    prim = p.parent / "PRIMCELL.vasp"
    if prim.exists():
        try:
            lat_in   = read_poscar(struct_path)
            lat_prim = read_poscar(str(prim))
            if not np.allclose(lat_in, lat_prim, atol=1e-3):
                # 同一个布拉维格子、只是基矢选得不一样？ # [vaspkit-auto]
                M = lat_prim @ np.linalg.inv(lat_in)
                same_lattice = (np.allclose(M, np.round(M), atol=1e-4)
                                and abs(abs(np.linalg.det(M)) - 1.0) < 1e-4)
                if same_lattice and VASPKIT_REBASE:
                    _rebase = (lat_in, lat_prim)
                    print("[..] PRIMCELL 与你的结构是同一格子的不同基矢选择"
                          "（变换矩阵整数、|det|=1），路径坐标将自动换算到你的 "
                          "POSCAR 基底，无需更换结构。", file=sys.stderr)
                elif same_lattice:
                    raise ValueError(
                        "PRIMCELL.vasp 与你的结构是同一格子的不同基矢选择，"
                        "但 VASPKIT_REBASE=False，拒绝自动换基。\n"
                        "        把 VASPKIT_REBASE 改成 True 即可。")
                else:
                    raise ValueError(
                    "POSCAR 的晶格与 vaspkit 的 PRIMCELL.vasp 不是同一个格子 ——\n"
                    "        （变换矩阵不是整数或 |det| != 1，说明体积/格子类型都变了，\n"
                    "         比如你的结构是超胞、或是常规胞而 PRIMCELL 是原胞。）\n"
                    "        KPATH.in 的坐标相对 PRIMCELL 写，直接拿来用会错。\n"
                    "        对策三选一：\n"
                    "          (a) 用 PRIMCELL.vasp 作为结构从 step1 重跑整条流程；\n"
                    "          (b) KPATH_METHOD 改回 'seekpath'（3D）或 'native_2d'（2D），\n"
                    "              它们会把路径点严格变换回你自己 POSCAR 的基底；\n"
                    "          (c) KPATH_METHOD='manual' 自己写面内路径。")
        except ValueError:
            raise
        except Exception as exc:                        # 读文件失败不致命
            print("[注意] 无法比对 PRIMCELL.vasp（%s），跳过晶格一致性检查" % exc,
                  file=sys.stderr)
    else:
        print("[注意] %s 旁边没有 PRIMCELL.vasp，无法验证 KPATH.in 的坐标基底。"
              "请自行确认 POSCAR 就是 vaspkit 标准化后的原胞。" % p.parent,
              file=sys.stderr)

    rows = []
    for ln in L[4:]:
        t = ln.split()
        if len(t) < 4:
            continue
        try:
            xyz = tuple(float(x) for x in t[:3])
        except ValueError:
            continue
        rows.append((xyz, t[3].lstrip("!").strip()))
    if len(rows) < 2 or len(rows) % 2:
        raise ValueError("%s 的高对称点行数是 %d（Line-Mode 要求成对出现）" % (p, len(rows)))

    coords, segments, cur = {}, [], []
    for i in range(0, len(rows), 2):
        (a, la), (b, lb) = rows[i], rows[i + 1]
        for lab, xyz in ((la, a), (lb, b)):
            if lab in coords and not np.allclose(coords[lab], xyz, atol=1e-6):
                raise ValueError("标签 %s 在 %s 里出现了两组不同坐标" % (lab, p))
            coords[lab] = np.array(xyz, dtype=float)
        if cur and cur[-1] == la:
            cur.append(lb)                  # 与上一条腿首尾相接 -> 同一段
        else:
            if len(cur) >= 2:
                segments.append(cur)
            cur = [la, lb]                  # 断开 -> 新起一段
    if len(cur) >= 2:
        segments.append(cur)
    if _rebase is not None:                               # [vaspkit-auto]
        # 走笛卡尔换基：k = f_prim @ B_prim，再 f_in_i = k · a_in_i / 2π
        lat_in, lat_prim = _rebase
        B_prim = TWOPI * np.linalg.inv(lat_prim).T
        coords = {lab: np.array([float((np.asarray(f) @ B_prim) @ lat_in[i] / TWOPI)
                                 for i in range(3)])
                  for lab, f in coords.items()}
    route = " | ".join("-".join(_pretty(l) for l in s) for s in segments)
    note  = "读自 %s：%s" % (p, route)
    if _rebase is not None:
        note += "（坐标已换算到你的 POSCAR 基底）"
    return coords, segments, "vaspkit", note


# =====================================================================
# 2D 原生高对称路径（原 kpath2d.py，已内联，无需额外文件） # [kpath2d-inline]
#
# 为什么不用 seekpath 再剔点：seekpath / Setyawan-Curtarolo 都按【三维布拉维
# 格子】给路径，它们不知道 c 是真空。先按 3D 算完再剔除面外点，会把一条连续
# 路径挖断成 `Γ-X | Γ-Z | U-Γ` 这种碎片，标签也沿用 3D 约定，没法和文献对照。
#
# 坐标怎么保证不写错：全程走【笛卡尔倒空间】，最后统一用 f_i = k·a_i / 2π
# 换回输入 POSCAR 的分数坐标。该式对任意（含非正交）晶胞都成立，不需要任何
# 整数变换矩阵的记账，从根上避免基底变换写错。真空方向分量会自动是 0。
# =====================================================================

# ---- 分类容差 ----
TOL_LEN = 1.0e-2      # 相对容差：|a-b|/max(a,b) 小于它认为等长
TOL_ANG = 1.0         # 角度容差（度）


# =====================================================================
# POSCAR 读取 / 真空轴判定
# =====================================================================
def read_lattice(poscar_path):
    """返回 3x3 晶格矩阵（行 = a1,a2,a3，单位 Ang）以及分数坐标。"""
    with open(poscar_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    scale = float(lines[1].split()[0])
    lat = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)],
                   dtype=float)
    if scale < 0:                       # 负数表示目标体积
        vol = abs(np.linalg.det(lat))
        scale = (abs(scale) / vol) ** (1.0 / 3.0)
    lat = lat * scale

    # 读分数坐标（仅用于真空判定）
    i = 5
    if not lines[i].split()[0].isdigit():   # 有元素符号行
        i += 1
    counts = [int(x) for x in lines[i].split()]
    nat = sum(counts)
    i += 1
    if lines[i].strip().upper().startswith(("S", "s")):   # Selective dynamics
        i += 1
    direct = lines[i].strip().upper().startswith(("D", "d"))
    i += 1
    pos = np.array([[float(x) for x in lines[i + k].split()[:3]]
                    for k in range(nat)], dtype=float)
    if not direct:
        pos = (pos * scale) @ np.linalg.inv(lat)
    return lat, pos % 1.0


def detect_vacuum_axis(lat, pos, vacuum_min=8.0):
    """沿哪条晶轴存在 >= vacuum_min Ang 的真空间隙。返回 axis 或 None。"""
    found = []
    for ax in range(3):
        h = np.linalg.norm(np.cross(lat[(ax + 1) % 3], lat[(ax + 2) % 3]))
        d_perp = abs(np.linalg.det(lat)) / h        # 该方向的层间距（垂直高度）
        s = np.sort(pos[:, ax])
        if len(s) == 1:
            gap_frac = 1.0
        else:
            gaps = np.diff(np.concatenate([s, [s[0] + 1.0]]))
            gap_frac = gaps.max()
        if gap_frac * d_perp >= vacuum_min:
            found.append((ax, gap_frac * d_perp))
    if len(found) == 0:
        return None
    if len(found) > 1:
        raise ValueError("检出 %d 个真空方向（1D/0D 体系），本模块只处理 2D"
                         % len(found))
    return found[0][0]


# =====================================================================
# 二维格子约化与分类
# =====================================================================
def gauss_reduce_2d(a1, a2, tol_len=TOL_LEN):
    """Lagrange-Gauss 约化：返回面内最短的一组基矢（笛卡尔 3 分量）。

    交换判据带 tol_len 容差：POSCAR 里 √3/2 之类的数只写到小数点后 6~10 位，
    六方/正方胞的 |a1| 与 |a2| 会差 ~1e-7，用严格比较会触发无谓的交换，
    结果虽然物理等价，但标签坐标会变成 (-0.5,0.5,0) 这种不好和文献对照的形式。
    """
    a1, a2 = np.array(a1, float), np.array(a2, float)
    for _ in range(100):
        if a1 @ a1 > a2 @ a2 * (1.0 + 2 * tol_len):
            a1, a2 = a2, a1
        # 用 floor(x+0.5) 而非 round()：round() 是 banker's rounding，
        # x=±0.5（正好是六方/正方的情形）会随浮点末位噪声跳变
        m = int(np.floor((a1 @ a2) / (a1 @ a1) + 0.5))
        if m == 0:
            break
        cand = a2 - m * a1
        if cand @ cand >= a2 @ a2 * (1.0 - 1e-12):
            break                       # 没真正变短就停，避免无穷振荡
        a2 = cand
    if a1 @ a2 > 0:                 # 统一取钝角，γ ∈ [90°, 120°]
        a2 = -a2
    return a1, a2


def dual_2d(a1, a2):
    """面内二维对偶基矢 b_i（笛卡尔 3 分量），满足 b_i·a_j = 2π δ_ij 且 b_i ⊥ n。"""
    n = np.cross(a1, a2)
    n = n / np.linalg.norm(n)
    b1 = TWOPI * np.cross(a2, n) / (a1 @ np.cross(a2, n))
    b2 = TWOPI * np.cross(n, a1) / (a2 @ np.cross(n, a1))
    return b1, b2


def classify_2d(a1, a2, tol_len=TOL_LEN, tol_ang=TOL_ANG):
    """返回 (类型字符串, a, b, gamma)。输入须是已约化并取钝角的基矢。"""
    a, b = np.linalg.norm(a1), np.linalg.norm(a2)
    gamma = np.degrees(np.arccos(np.clip((a1 @ a2) / (a * b), -1, 1)))
    eq = abs(a - b) / max(a, b) < tol_len
    is90 = abs(gamma - 90.0) < tol_ang
    is120 = abs(gamma - 120.0) < tol_ang
    if eq and is120:
        kind = "hexagonal"
    elif eq and is90:
        kind = "square"
    elif is90:
        kind = "rectangular"
    elif eq:
        kind = "centered_rectangular"
    else:
        kind = "oblique"
    return kind, a, b, gamma


# =====================================================================
# 主入口
# =====================================================================
#  每种二维格子的标准闭合路径：{标签: (f1, f2)}，坐标以下方 basis 为准
_PATHS = {
    "hexagonal":            ({"G": (0, 0), "M": (0.5, 0.0), "K": (1/3, 1/3)},
                             ["G", "M", "K", "G"]),
    "square":               ({"G": (0, 0), "X": (0.5, 0.0), "M": (0.5, 0.5)},
                             ["G", "X", "M", "G"]),
    "rectangular":          ({"G": (0, 0), "X": (0.5, 0.0),
                              "S": (0.5, 0.5), "Y": (0.0, 0.5)},
                             ["G", "X", "S", "Y", "G"]),
    "centered_rectangular": ({"G": (0, 0), "X": (0.5, 0.0),
                              "S": (0.5, 0.5), "Y": (0.0, 0.5)},
                             ["G", "X", "S", "Y", "G"]),
    "oblique":              ({"G": (0, 0), "X": (0.5, 0.0),
                              "S": (0.5, 0.5), "Y": (0.0, 0.5)},
                             ["G", "X", "S", "Y", "G"]),
}


def kpath_2d(poscar_path, vacuum_min=8.0, vac_axis=None,
             tol_len=TOL_LEN, tol_ang=TOL_ANG, verbose=True):
    """
    返回 (labels, segments, method, note)，与 gen_step3 的 auto_kpath 完全同构。
      labels  : {label: (f1, f2, f3)}  以【输入 POSCAR 的倒格子】为基
      segments: [[lab, lab, ...]]      只有一段，首尾都是 Γ，天然连续闭合
    """
    lat, pos = read_lattice(poscar_path)
    if vac_axis is None:
        vac_axis = detect_vacuum_axis(lat, pos, vacuum_min)
        if vac_axis is None:
            raise ValueError("没有检出 >= %.1f Ang 的真空层，这不像 2D 体系；"
                             "若确定是 2D，请显式传 vac_axis。" % vacuum_min)
    inplane = [i for i in range(3) if i != vac_axis]

    # 1) 面内基矢约化 + 分类
    a1r, a2r = gauss_reduce_2d(lat[inplane[0]], lat[inplane[1]], tol_len)
    kind, a, b, gamma = classify_2d(a1r, a2r, tol_len, tol_ang)

    # 2) 有心矩形改用正交的常规胞（与文献画法一致）
    basis_note = "约化原胞面内基矢"
    if kind == "centered_rectangular":
        A, B = a1r + a2r, a1r - a2r          # 钝角时 |A| < |B|，且 A ⊥ B
        a1r, a2r = (A, B) if A @ A <= B @ B else (B, A)
        basis_note = "有心矩形的正交常规胞 (a1±a2)"

    # 3) 面内对偶基矢（笛卡尔）
    b1, b2 = dual_2d(a1r, a2r)

    # 4) 标准路径 -> 笛卡尔 k -> 输入 POSCAR 倒格子分数坐标
    pts2d, seg = _PATHS[kind]
    labels = {}
    for lab, (f1, f2) in pts2d.items():
        kcart = f1 * b1 + f2 * b2
        labels[lab] = tuple(float(kcart @ lat[i] / TWOPI) for i in range(3))

    # 5) 自检：真空方向分量必须是 0（真空轴不垂直于面时会非零，属正常，仅提示）
    max_out = max(abs(v[vac_axis]) for v in labels.values())

    note = ("2D 原生路径 | %s | a=%.4f Ang, b=%.4f Ang, γ=%.2f° | 真空轴=%s | %s"
            % (kind, a, b, gamma, "abc"[vac_axis], basis_note))
    if kind == "oblique":
        note += " | ⚠ 斜方格子无公认标准路径，此为近似，务必与文献核对"
    if max_out > 1e-8:
        note += " | 注意：真空轴与面内不垂直，k 点第 %d 分量非零(%.2e)，这是正确的" \
                % (vac_axis + 1, max_out)

    if verbose:
        print("[..] " + note)
        for lab in seg:
            v = labels[lab]
            kc = np.array(v) @ (TWOPI * np.linalg.inv(lat).T)
            d = kc / np.linalg.norm(kc) if np.linalg.norm(kc) > 1e-12 else kc
            print("     %-3s = (%+.6f %+.6f %+.6f)   |k| = %.4f Ang^-1   "
                  "笛卡尔方向 (%+.3f %+.3f %+.3f)"
                  % (lab, v[0], v[1], v[2], np.linalg.norm(kc), *d))

    return labels, [list(seg)], "native_2d", note


# =====================================================================
# 可选：对称性交叉校验（装了 pymatgen 才跑）
# =====================================================================
def check_symmetry(poscar_path, kind, symprec=1e-2, angle_tol=5.0):
    """
    格子度规是六方/正方，不代表原子基元也保持该对称性（如 1T′ 畸变相）。
    此时 Γ-M-K-Γ 可能不足以覆盖不可约区。装了 pymatgen 就做一次提醒。
    """
    try:
        from pymatgen.core import Structure
        from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    except ImportError:
        return None
    st = Structure.from_file(poscar_path)
    sga = SpacegroupAnalyzer(st, symprec=symprec, angle_tolerance=angle_tol)
    pg = sga.get_point_group_symbol()
    sg = "%s (#%d)" % (sga.get_space_group_symbol(), sga.get_space_group_number())
    expect = {"hexagonal": {"6/mmm", "6/m", "-6m2", "6mm", "622", "-3m", "3m", "32", "-3", "3", "6", "-6"},
              "square":    {"4/mmm", "4/m", "-42m", "4mm", "422", "4", "-4"}}
    warn = None
    if kind in expect and pg not in expect[kind]:
        warn = ("⚠ 面内格子度规是 %s，但实际点群是 %s（空间群 %s）——"
                "原子基元破坏了格子对称性，标准路径可能不足以覆盖不可约区，"
                "建议改用 KPATH_METHOD='manual' 补上额外分段。" % (kind, pg, sg))
    return warn


# =====================================================================
# --preview-kpath：投任务前先看一眼路径（不生成任何文件，看完就退出） # [kpath2d-inline]
# =====================================================================
def _kpath_metrics(poscar):
    lat, pos = read_lattice(poscar)
    vac = detect_vacuum_axis(lat, pos, vacuum_min=8.0)
    if vac is None:
        raise ValueError("没检出 >= 8 Ang 的真空层 —— 这看起来是 3D 体相，"
                         "路径请交给 seekpath / vaspkit -task 303")
    ip = [i for i in range(3) if i != vac]
    a1r, a2r = gauss_reduce_2d(lat[ip[0]], lat[ip[1]])
    kind, a, b, gamma = classify_2d(a1r, a2r)
    labels, segments, _, note = kpath_2d(poscar, verbose=False)
    return kind, labels, segments, note, lat, (a, b, gamma), vac


def _preview_show(poscar):
    kind, labels, segments, note, lat, (a, b, g), vac = _kpath_metrics(poscar)
    recip = TWOPI * np.linalg.inv(lat).T
    seg = segments[0]
    print("=" * 68)
    print("结构      : %s" % poscar)
    print("面内格子  : %s   a=%.4f Ang  b=%.4f Ang  γ=%.2f°" % (kind, a, b, g))
    print("真空轴    : %s" % "abc"[vac])
    print("路径      : %s" % " - ".join("Γ" if x == "G" else x for x in seg))
    print("-" * 68)
    print("%-4s %-36s %-11s %s" % ("点", "分数坐标(输入 POSCAR 倒格子)",
                                   "|k| (Ang^-1)", "笛卡尔方向"))
    for lab in seg[:-1]:
        v = np.array(labels[lab])
        kc = v @ recip
        nk = np.linalg.norm(kc)
        d = kc / nk if nk > 1e-12 else kc
        print("%-4s (%+.6f %+.6f %+.6f)     %8.4f    (%+.3f %+.3f %+.3f)"
              % ("Γ" if lab == "G" else lab, v[0], v[1], v[2], nk, *d))
    print("-" * 68)
    legs, tot = [], 0.0
    for i in range(len(seg) - 1):
        p0 = np.array(labels[seg[i]]) @ recip
        p1 = np.array(labels[seg[i + 1]]) @ recip
        d = np.linalg.norm(p1 - p0)
        legs.append((seg[i], seg[i + 1], d))
        tot += d
    for l0, l1, d in legs:
        print("  %-2s→%-2s  %7.4f Ang^-1  %5.1f%%  %s"
              % (l0, l1, d, 100 * d / tot, "█" * max(1, int(round(30 * d / tot)))))
    print("  总长    %7.4f Ang^-1" % tot)
    if "⚠" in note:
        print("-" * 68)
        print("[警告] " + note.split("⚠")[1].split("|")[0].strip())


def _preview_compare(f1, f2):
    k1, l1, s1, _, _, m1, _ = _kpath_metrics(f1)
    k2, l2, s2, _, _, m2, _ = _kpath_metrics(f2)
    n1, n2 = os.path.basename(f1), os.path.basename(f2)
    print("=" * 68)
    print("%-12s %-24s %s" % ("", n1, n2))
    print("%-12s %-24s %s" % ("格子类型", k1, k2))
    print("%-12s %-24s %s" % ("a (Ang)", "%.4f" % m1[0], "%.4f" % m2[0]))
    print("%-12s %-24s %s" % ("b (Ang)", "%.4f" % m1[1], "%.4f" % m2[1]))
    print("%-12s %-24s %s" % ("γ (°)", "%.2f" % m1[2], "%.2f" % m2[2]))
    print("%-12s %-24s %s" % ("路径", "-".join(s1[0]), "-".join(s2[0])))
    print("-" * 68)
    if k1 != k2:
        print("[结论] ✗ 格子类型变了（%s -> %s）——" % (k1, k2))
        print("       弛豫前算的路径已作废，必须用弛豫后的结构重新生成。")
        return 1
    dmax = max(abs(np.array(l1[l]) - np.array(l2[l])).max()
               for l in l1 if l in l2)
    if dmax > 1e-6:
        print("[结论] ✗ 类型没变但高对称点坐标变了（最大差 %.2e）——" % dmax)
        print("       多半是弛豫改变了面内基矢的约化结果，请用弛豫后的结构重新生成。")
        return 1
    print("[结论] ✓ 格子类型与高对称点分数坐标一致，路径可以放心复用。")
    print("       （晶格常数变了不要紧，高对称点分数坐标只取决于格子类型）")
    return 0


def _preview_kpath_cli(argv):
    """--preview-kpath [结构1] [结构2]；给两个结构则进入弛豫前后对比模式。"""
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        for c in (os.path.join(STEP2_DIR, "CONTCAR"),
                  os.path.join(STEP2_DIR, "POSCAR"),
                  "CONTCAR", "POSCAR"):
            if os.path.exists(c):
                files = [c]
                break
    if not files:
        sys.exit("[错误] 没找到结构文件。用法：\n"
                 "  python gen_step3_WAVECAR.py --preview-kpath [POSCAR]\n"
                 "  python gen_step3_WAVECAR.py --preview-kpath POSCAR step1_PBE_opt/CONTCAR")
    try:
        if len(files) >= 2:
            return _preview_compare(files[0], files[1])
        _preview_show(files[0])
        return 0
    except Exception as exc:
        sys.exit("[错误] %s" % exc)


def auto_kpath(struct_path, dim="3d"):
    """
    返回 (labels, segments, method, note)
      labels  : {label: (f1,f2,f3)}  —— 【输入 POSCAR 的倒格子基底】
      segments: [[lab, lab, ...], ...]  段内连续，段间跳变
    """
    # ---- 2D 原生路径 ---- # [kpath2d-inline]
    _use_2d = (KPATH_METHOD == "native_2d") or (
        dim == "2d" and AUTO_2D_KPATH
        and KPATH_METHOD in ("seekpath", "setyawan_curtarolo"))
    if _use_2d:
        labels, segments, method, note = kpath_2d(struct_path)
        try:
            warn = check_symmetry(struct_path, note.split("|")[1].strip(),
                                  symprec=SYMPREC, angle_tol=ANGLE_TOL)
        except Exception:
            warn = None
        if warn:
            print("[警告] " + warn, file=sys.stderr)
            note += " | " + warn
        if KPATH_METHOD != "native_2d":
            note += " |（dim=2D 且 AUTO_2D_KPATH=True，已从 %s 自动切换）" % KPATH_METHOD
        return labels, segments, method, note

    if KPATH_METHOD == "vaspkit":
        kfile = VASPKIT_KPATH_FILE                        # [vaspkit-auto]
        if RUN_VASPKIT_KPATH:
            kfile = run_vaspkit_kpath(VASPKIT_EXE, struct_path, dim)
        return kpath_from_vaspkit(kfile, struct_path)

    if KPATH_METHOD == "manual":
        if not MANUAL_KPOINTS or not MANUAL_SEGMENTS:
            raise ValueError("KPATH_METHOD='manual' 但 MANUAL_KPOINTS/MANUAL_SEGMENTS 是空的")
        missing = {l for seg in MANUAL_SEGMENTS for l in seg} - set(MANUAL_KPOINTS)
        if missing:
            raise ValueError("MANUAL_SEGMENTS 里这些标签没有坐标: %s" % ", ".join(sorted(missing)))
        return dict(MANUAL_KPOINTS), [list(s) for s in MANUAL_SEGMENTS], "manual", "手写路径"

    from pymatgen.core import Lattice, Structure
    struct = Structure.from_file(struct_path)

    method = KPATH_METHOD
    if method == "seekpath":
        try:
            import seekpath  # noqa: F401
        except ImportError:
            method = "setyawan_curtarolo"

    if method == "seekpath":
        from pymatgen.symmetry.kpath import KPathSeek
        kp = KPathSeek(struct, symprec=SYMPREC, angle_tolerance=ANGLE_TOL)
        B_pr = np.asarray(kp._rec_lattice.matrix)          # 含 2π
        lat_prim = Lattice(TWOPI * np.linalg.inv(B_pr).T)
        labels_prim = kp.kpath["kpoints"]
        segments = [list(s) for s in kp.kpath["path"]]
        note = "seekpath / HPKOT (Hinuma et al.)"
    else:
        from pymatgen.symmetry.kpath import KPathSetyawanCurtarolo
        kp = KPathSetyawanCurtarolo(struct, symprec=SYMPREC,
                                    angle_tolerance=ANGLE_TOL)
        lat_prim = kp.prim.lattice
        labels_prim = kp.kpath["kpoints"]
        segments = [list(s) for s in kp.kpath["path"]]
        note = "pymatgen / Setyawan-Curtarolo"
        if KPATH_METHOD == "seekpath":
            note += "（seekpath 没装，已自动退回；pip install seekpath 可用更全的 HPKOT 约定）"

    labels_in, detM = _to_input_basis(struct, labels_prim, lat_prim)
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
    sga = SpacegroupAnalyzer(struct, symprec=SYMPREC, angle_tolerance=ANGLE_TOL)
    note = "%s | 空间群 %s (#%d) | det M = %+.0f" % (
        note, sga.get_space_group_symbol(), sga.get_space_group_number(), detM)
    return labels_in, segments, method, note


def build_path(vertices3d, lat, line_density):
    """vertices3d: [(label, (f1,f2,f3)), ...]，连续折线。返回 (pts, labels, seglens)。"""
    recip = 2 * np.pi * np.linalg.inv(lat).T
    pts, labels, seglens = [], {}, []
    for s in range(len(vertices3d) - 1):
        (l0, f0), (l1, f1) = vertices3d[s], vertices3d[s + 1]
        c0, c1 = np.array(f0) @ recip, np.array(f1) @ recip
        dist   = np.linalg.norm(c1 - c0)
        seglens.append((l0, l1, dist))
        ndiv = max(1, int(round(dist * line_density)))
        for n in range(ndiv):
            frac = np.array(f0) + (np.array(f1) - np.array(f0)) * n / ndiv
            if n == 0:
                labels[len(pts)] = l0
            pts.append(frac)
    labels[len(pts)] = vertices3d[-1][0]
    pts.append(np.array(vertices3d[-1][1]))
    return pts, labels, seglens


def read_ibzkpt(path):
    with open(path) as f:
        lines = f.readlines()
    nk = int(lines[1].split()[0])
    pts = []
    for i in range(3, 3 + nk):
        t = lines[i].split()
        pts.append((float(t[0]), float(t[1]), float(t[2]), int(t[3])))
    return pts


def parse_incar(path):
    items = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s[0] in "#!":
                continue
            for c in ("#", "!"):
                if c in s:
                    s = s.split(c, 1)[0].strip()
            if "=" not in s:
                continue
            for part in s.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    items.append((k.strip().upper(), v.strip()))
    return items


def sanitize_label(text):
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return label.strip("_.-") or "material"


def read_structure_label(path):
    with open(path, encoding="utf-8-sig") as f:
        first = f.readline().strip()
    token = first.split()[0] if first.split() else "material"
    return sanitize_label(token)


def detect_method(items, method_file=None):
    """优先读 workflow_method.txt 的 FUNC=；缺失时回退为嗅探 INCAR。"""
    if method_file and os.path.exists(method_file):
        for line in open(method_file, encoding="utf-8"):
            if line.startswith("FUNC="):
                func = line.split("=", 1)[1].strip()
                if func in SUPPORTED_FUNCS:
                    return func
                break
    data = {k: v for k, v in items}
    gga = data.get("GGA", "").upper()
    ivdw = data.get("IVDW", "").split()[0] if data.get("IVDW") else None
    if gga == "PS" and ivdw is None:
        return "pbesol"
    if gga == "PE" and ivdw == "12":
        return "pbe-d3"
    if gga == "PE" and ivdw is None:
        return "pbe"
    raise SystemExit(
        "[错误] step2 不是受支持的统一方法：GGA=%s, IVDW=%s。"
        "支持的方法：%s。" % (gga or "缺失", ivdw or "未启用",
                             ", ".join(SUPPORTED_FUNCS))
    )


def build_step3_incar(src_items, remove, setd):
    remove   = {k.upper() for k in remove}
    setd     = {k.upper(): v for k, v in setd.items()}
    src_keys = {k for k, _ in src_items}
    body, seen = [], set()
    for k, v in src_items:
        if k in remove or k == "SYSTEM":
            continue
        body.append((k, setd[k] if k in setd else v))
        seen.add(k)
    # ★ 修正: 原来的条件是 `k not in seen and k not in src_keys`。
    #   若某个键同时出现在 remove 和 setd 里，且 step2 的 INCAR 也有它，
    #   则它在上面的循环被 remove 跳过（没进 seen），又因为在 src_keys 里
    #   而不会被补写 —— 结果整条键凭空消失。
    #   典型受害者: SOC 分支强制的 NCORE=1、本版新加的 KPAR。
    #   语义应当是"setd 永远赢"。
    for k, v in setd.items():
        if k != "SYSTEM" and k not in seen:
            body.append((k, v))
    lines = []
    if "SYSTEM" in setd:
        lines.append("SYSTEM = %s" % setd["SYSTEM"])
    for k, v in body:
        lines.append("%-8s = %s" % (k, v))
    return "\n".join(lines) + "\n"


def is_automatic_kpoints(path):
    """判断一个 KPOINTS 是否为自动网格格式（第 2 行为 0，第 3 行 G/M 开头）。
       只有自动网格才能交给 VASP 自行按本步的 ISYM 约化。"""
    try:
        L = Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        if len(L) < 4:
            return False
        if int(L[1].split()[0]) != 0:
            return False
        return L[2].strip()[:1].upper() in ("G", "M", "A")
    except (OSError, ValueError, IndexError):
        return False


def guess_total_cores(step_dir):
    """从 submit.sh 猜总 MPI 核数：优先 mpirun -np <数字>，
       其次 SBATCH 的 nodes×ntasks-per-node，再次 --ntasks。
       注意 `mpirun -np $SLURM_NTASKS` 不是数字，会自动落到 SBATCH 分支。"""
    path = os.path.join(step_dir, "submit.sh")
    if not os.path.exists(path):
        return None, "找不到 %s/submit.sh" % step_dir
    txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(?:mpirun|mpiexec|srun)[^\n]*?\s-(?:np|n)\s+(\d+)", txt)
    if m:
        return int(m.group(1)), "mpirun -np"
    nodes = re.search(r"(?:--nodes|-N)[=\s]+(\d+)", txt)
    tpn   = re.search(r"ntasks-per-node[=\s]+(\d+)", txt)
    ntot  = re.search(r"--ntasks[=\s]+(\d+)", txt)
    if nodes and tpn:
        return int(nodes.group(1)) * int(tpn.group(1)), "nodes×ntasks-per-node"
    if ntot:
        return int(ntot.group(1)), "--ntasks"
    return None, "无法从 submit.sh 解析核数"


def detect_metallicity(step2_dir, gap_tol=None):
    """读 step2 的 EIGENVAL 判断金属 / 半导体。

    判据是物理定义本身：只要存在某条能带 n，它在一部分 k 点上被占据、
    在另一部分 k 点上空着（即该带跨越费米面），体系就是金属。
    用占据数而非费米能来判断，避免了不同 ISMEAR 下 E-fermi 定义不一致的麻烦。

    返回 (is_metal, gap, note)。读不到文件时返回 (None, None, 原因)。
    """
    tol = METAL_GAP_TOL if gap_tol is None else gap_tol
    p = os.path.join(step2_dir, "EIGENVAL")
    if not os.path.exists(p):
        return None, None, "找不到 %s" % p
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            L = [ln.split() for ln in f]
        ispin = int(L[0][3])
        nkpts, nbands = int(L[5][1]), int(L[5][2])
        # 每个 k 点块：1 空行 + 1 坐标行 + nbands 条本征值
        bands = {}                     # band_index -> [(E, occ), ...]
        i = 6
        for _ in range(nkpts):
            while i < len(L) and len(L[i]) < 4:
                i += 1                 # 跳到坐标行（kx ky kz weight）
            i += 1
            for b in range(nbands):
                t = L[i + b]
                n = int(t[0])
                if ispin == 2:
                    pairs = [(float(t[1]), float(t[3])), (float(t[2]), float(t[4]))]
                else:
                    pairs = [(float(t[1]), float(t[2]))]
                bands.setdefault(n, []).extend(pairs)
            i += nbands
    except Exception as exc:
        return None, None, "解析 EIGENVAL 失败：%s" % exc

    occ_max = max(o for v in bands.values() for _, o in v)
    if occ_max <= 0:
        return None, None, "EIGENVAL 里占据数全为 0，无法判断"
    half = 0.5 * occ_max

    full, empty = [], []
    for n, v in bands.items():
        os_ = [o for _, o in v]
        if max(os_) > half and min(os_) < half:
            e = [E for E, _ in v]
            return True, 0.0, ("第 %d 条带跨越费米面（该带占据数 %.2f~%.2f，"
                               "能量 %.3f~%.3f eV）-> 金属"
                               % (n, min(os_), max(os_), min(e), max(e)))
        (full if max(os_) > half else empty).append([E for E, _ in v])
    if not full or not empty:
        return None, None, "所有能带同为占据或同为空，NBANDS 可能不足"
    gap = min(min(e) for e in empty) - max(max(e) for e in full)
    if gap < tol:
        return True, gap, "带隙 %.4f eV < %.2f eV 阈值 -> 按金属处理" % (gap, tol)
    return False, gap, "带隙 %.4f eV -> 半导体/绝缘体" % gap


def resolve_smearing(step2_dir, step2_items):
    """按 SMEARING_MODE 定出 (ISMEAR, SIGMA, note)。"""
    if SMEARING_MODE == "semiconductor":
        return "0", SIGMA_SEMI, "强制半导体档"
    if SMEARING_MODE == "metal":
        return "1", SIGMA_METAL, "强制金属档"
    if SMEARING_MODE == "inherit":
        d = {k.upper(): v for k, v in step2_items}
        ism, sig = d.get("ISMEAR", "0"), d.get("SIGMA", SIGMA_SEMI)
        if ism.strip().split()[0] in ("-5", "-4"):
            return "0", SIGMA_SEMI, ("step2 是 ISMEAR=%s（四面体法），本步 KPOINTS "
                                     "没有 Tetrahedra 块会罢工，已强制改为 0" % ism)
        return ism, sig, "继承 step2 的 ISMEAR/SIGMA"
    metal, gap, note = detect_metallicity(step2_dir)
    if metal is None:
        return "0", SIGMA_SEMI, "自动判定失败（%s），回退半导体档" % note
    if metal:
        return "1", SIGMA_METAL, "自动判定：%s" % note
    return "0", SIGMA_SEMI, "自动判定：%s" % note


def auto_parallel(step_dir, nk_scf, nbands):
    """为 KPOINTS_OPT 方案挑一个合法的 KPAR。
       返回 (kpar, npar_group, nbands_adjusted, note)。
       KPOINTS_OPT 下 NCORE 恒为 1，故组内 NPAR = 总核数 / KPAR。"""
    cores, src = guess_total_cores(step_dir)
    if not cores:
        return None, None, nbands, src
    cap = KPAR_MAX
    if nk_scf:
        cap = min(cap, nk_scf)          # KPAR 超过自洽 k 点数只会让部分组闲着
    cap = max(1, cap)
    # 两遍搜索：先找"KPAR 尽量大 且 NBANDS 正好能被组内 NPAR 整除"的解；
    # 找不到就退而求其次，只保证 KPAR 尽量大，再把 NBANDS 向上取整。
    # （不能反过来先保 NBANDS —— 那会在 96 核这种情况下把 KPAR 压到 1，
    #   变成 NPAR=96 的病态分解，正是旧版要避免的那种崩法。）
    divisors = [c for c in range(cap, 0, -1) if cores % c == 0]
    kpar = None
    for cand in divisors:
        if nbands and nbands % (cores // cand):
            continue
        kpar = cand
        break
    if kpar is None:
        kpar = divisors[0] if divisors else 1
    npar = cores // kpar
    nb = nbands
    if nb and nb % npar:
        nb = int(np.ceil(nb / npar) * npar)
    return kpar, npar, nb, "%d 核（%s）" % (cores, src)


def render_submit(tpl_path, out_path, params):
    """从 skill 目录的提交模板渲染 submit.sh（只填 {{JOBNAME}}）。"""
    if not os.path.exists(tpl_path):
        raise SystemExit("[错误] 找不到提交模板 %s" % tpl_path)
    with open(tpl_path) as f:
        text = f.read()
    for key, val in params.items():
        text = text.replace("{{" + key + "}}", str(val))
    leftover = set(re.findall(r"\{\{(\w+)\}\}", text))
    if leftover:
        raise SystemExit("[错误] %s 仍有未填充占位符：%s"
                         "（本脚本只填 JOBNAME，其余参数请直接固化在模板中）"
                         % (tpl_path, leftover))
    with open(out_path, "w") as f:
        f.write(text)
def read_atom_counts(struct):
    with open(struct) as f:
        L = f.readlines()
    idx = 5
    if not L[idx].split()[0].replace('-', '').isdigit():
        idx += 1
    return [int(x) for x in L[idx].split()]


def read_zvals(potcar):
    zvals = []
    with open(potcar) as f:
        for line in f:
            if "ZVAL" in line:
                m = re.search(r"ZVAL\s*=\s*([0-9.]+)", line)
                if m:
                    zvals.append(float(m.group(1)))
    return zvals


def resolve_nbands(struct, ispin, step2_dir, soc):
    if isinstance(STEP3_NBANDS, int):
        return STEP3_NBANDS, "手动指定"
    outcar = os.path.join(step2_dir, OUTCAR_FILE)
    if os.path.exists(outcar):
        val = None
        with open(outcar) as f:
            for line in f:
                if "NBANDS=" in line:
                    m = re.search(r"NBANDS=\s*(\d+)", line)
                    if m:
                        val = int(m.group(1))
        if val:
            if soc:
                val = int(np.ceil(val * 2 / NBANDS_ROUND) * NBANDS_ROUND)
                return val, "读自 %s（共线值 ×2 for SOC）" % outcar
            return val, "读自 %s" % outcar
    potcar = os.path.join(step2_dir, POTCAR_FILE)
    if os.path.exists(potcar):
        counts = read_atom_counts(struct)
        zvals  = read_zvals(potcar)
        if zvals and len(zvals) == len(counts):
            nelect = sum(c * z for c, z in zip(counts, zvals))
            nions  = sum(counts)
            if soc:
                base = nelect + nions                     # 非共线：占据态数 = NELECT
            elif ispin == 1:
                base = nelect / 2 + nions / 2
            else:
                base = nelect * 0.6 + nions
            nb     = int(np.ceil(base / NBANDS_ROUND) * NBANDS_ROUND)
            return nb, "由 POTCAR/POSCAR 估算(NELECT=%g, NIONS=%d%s)" % (
                nelect, nions, ", ×2 SOC" if soc else "")
    return None, "无法确定（缺 OUTCAR 与 POTCAR）"


def read_species_and_counts(struct):
    """从 POSCAR 读 (元素符号列表, 各元素原子数)。
       VASP5: 第6行=符号, 第7行=计数；VASP4(无符号行)则符号退回 None。"""
    with open(struct, encoding="utf-8-sig") as f:
        L = f.readlines()
    line6 = L[5].split()
    if line6 and line6[0].lstrip("-").isdigit():
        return None, [int(x) for x in line6]          # 无符号行
    return line6, [int(x) for x in L[6].split()]


def read_magnetization(outcar, nions):
    """从 step2 OUTCAR 读【最后一个】'magnetization (x)' 块里每离子的 tot 磁矩。
       共线 ISPIN=2 才有；返回长度 nions 的列表，或 None（非磁/无块/不完整）。"""
    if not outcar or not os.path.exists(outcar):
        return None
    with open(outcar, errors="ignore") as f:
        lines = f.readlines()
    last = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("magnetization (x)"):
            last = i
    if last is None:
        return None
    moms, seen = [], False
    for ln in lines[last + 1:]:
        t = ln.split()
        if t and t[0].isdigit():                       # 数据行以离子号开头
            moms.append(float(t[-1]))                   # tot 在最后一列
            seen = True
        elif seen:                                      # 'tot' 汇总行 -> 块结束
            break
    return moms[:nions] if len(moms) >= nions else None


def expand_per_species(per_species, symbols, counts):
    """{'Mn':5,'Se':0} + 符号/计数 -> 每离子磁矩列表；缺符号行或缺某元素则 None。"""
    if not per_species or symbols is None:
        return None
    out = []
    for sym, n in zip(symbols, counts):
        if sym not in per_species:
            return None
        out.extend([float(per_species[sym])] * n)
    return out


def default_moments(symbols, counts):
    """按 MAG_ELEM_MOMENTS 给每离子高自旋起点；无磁性候选元素则 None。"""
    if symbols is None:
        return None
    if not any(MAG_ELEM_MOMENTS.get(s, 0.0) != 0.0 for s in symbols):
        return None
    out = []
    for sym, n in zip(symbols, counts):
        out.extend([float(MAG_ELEM_MOMENTS.get(sym, 0.0))] * n)
    return out


def resolve_soc(struct):
    """SOC="auto" 时按 POSCAR 元素判定；返回 (soc_bool, note)。"""
    if SOC is not True and SOC is not False:  # "auto"
        symbols, _ = read_species_and_counts(struct)
        heavy = sorted(set(symbols or []) & SOC_ELEMS)
        if heavy:
            return True, "auto: 检测到重元素 %s (Z>=50)" % "/".join(heavy)
        return False, "auto: 无 Z>=50 重元素"
    return bool(SOC), "手动指定 SOC=%s" % SOC


def build_magmom(soc, magnetic, moments, nions, saxis):
    """返回 (magmom_str_or_None, ispin_or_None, extra_tags, note)。
       moments: 每离子共线磁矩列表（magnetic=True 时须非 None）。"""
    tags = {}
    if not magnetic:
        if soc:
            return "%d*0.0" % (3 * nions), None, tags, "非磁 MAGMOM=%d*0.0" % (3 * nions)
        return None, "1", tags, "非磁 (ISPIN=1，不写 MAGMOM)"
    if soc:
        # 非共线：每离子 3 分量，磁矩置于 SAXIS 方向（默认 z）
        magstr = "  ".join("0 0 %g" % m for m in moments)
        tags["SAXIS"] = "%g %g %g" % tuple(saxis)
        return magstr, None, tags, "磁性·非共线 MAGMOM(3*%d 沿 SAXIS)，已剔除 ISPIN" % nions
    # 磁性·共线（无 SOC）：每离子 1 个，ISPIN=2
    return " ".join("%g" % m for m in moments), "2", tags, "磁性·共线 MAGMOM(%d)，ISPIN=2" % nions


def main():
    if "--preview-kpath" in sys.argv:          # [kpath2d-inline]
        sys.exit(_preview_kpath_cli(sys.argv[1:]))

    if not os.path.isdir(STEP2_DIR):
        sys.exit("[错误] 找不到 %s —— 请在父目录下运行本脚本" % STEP2_DIR)

    # 结构文件
    struct = STRUCT_FILE
    if struct is None:
        for c in ("CONTCAR", "POSCAR"):
            p = os.path.join(STEP2_DIR, c)
            if os.path.exists(p):
                struct = p
                break
    if struct is None:
        sys.exit("[错误] %s 里找不到 CONTCAR/POSCAR" % STEP2_DIR)
    label = read_structure_label(struct)

    # ---- SOC 判定（"auto" 按 POSCAR 元素）----
    soc, soc_note = resolve_soc(struct)

    # ---- 维度：优先继承 step2 workflow_method.txt 的 DIM=，缺失按结构判定 ----
    dim, dim_note = resolve_dim(os.path.join(STEP2_DIR, METHOD_FILE), struct)
    require_dim(dim, ('2d', '3d'), "step3_wavecar",
                why="高对称路径定义在晶体倒空间，孤立分子没有能带")
    print("[..] 维度：%s — %s" % (dim.upper(), dim_note), file=sys.stderr)

    ibzkpt = os.path.join(STEP2_DIR, IBZKPT_FILE)
    if not os.path.exists(ibzkpt):
        sys.exit("[错误] 找不到 %s —— 请先跑完 step2 PBE 静态" % ibzkpt)

    lat = read_poscar(struct)

    # ---- 高对称路径：通用晶系自动识别，坐标已变换到输入 POSCAR 的倒格子基底 ----
    try:
        kpt_coords, segments, kmethod, knote = auto_kpath(struct, dim)  # [kpath2d-inline]
    except Exception as exc:
        sys.exit("[错误] 高对称路径生成失败: %s\n"
                 "        对策: (1) 放宽 SYMPREC（1e-2 -> 5e-2）；"
                 "(2) 换 KPATH_METHOD='setyawan_curtarolo'；"
                 "(3) KPATH_METHOD='manual' 自己写 MANUAL_KPOINTS/MANUAL_SEGMENTS。" % exc)
    ltype, info = kmethod, knote

    # ---- 2D：剔除真空方向 (kz) 分量非零的高对称点，并按剔除点切断分段 ----
    if dim == "2d" and FILTER_KPATH_2D and kmethod != "native_2d":  # [kpath2d-inline]
        kpt_coords, segments, dropped = filter_kpath_2d(kpt_coords, segments, axis=2)
        if dropped:
            print("[..] 2D 路径过滤：剔除面外高对称点 %s（kz≠0，对 2D 无意义）"
                  % "/".join(dropped), file=sys.stderr)
        if not any(len(seg) >= 2 for seg in segments):
            sys.exit("[错误] 2D 路径过滤后没有有效分段 —— 可能真空方向不是 c 轴，"
                     "或 KPATH_METHOD 给的路径全在面外。请改用 KPATH_METHOD='manual' "
                     "手写面内路径（如六方 Γ-M-K-Γ）。")

    # 逐段构建路径（段间为跳变）
    path_pts, path_labels = [], {}   # path_labels: 路径内全局索引 -> 标签
    seglens_main, running = None, 0
    route, breaks = [], []
    for si, seg in enumerate(segments):
        verts = [(lab, kpt_coords[lab]) for lab in seg]
        pts, labels, seglens = build_path(verts, lat, LINE_DENSITY)
        for li, lab in labels.items():
            path_labels[running + li] = lab
        if si > 0:
            breaks.append(running)       # 该索引处与前一点之间是跳变
        path_pts.extend(pts)
        running += len(pts)
        route.append("-".join(_pretty(l) for l in seg))
        if si == 0:
            seglens_main = seglens

    uni   = read_ibzkpt(ibzkpt)
    extra = [(lab, np.array(f, dtype=float)) for lab, f in EXTRA_KPTS]
    nuni, npath, nextra = len(uni), len(path_pts), len(extra)

    # 写 KPOINTS (+ KPOINTS_OPT) 到 step3
    os.makedirs(STEP3_DIR, exist_ok=True)
    kpoints_file = os.path.join(STEP3_DIR, "KPOINTS")
    kopt_file    = os.path.join(STEP3_DIR, "KPOINTS_OPT")

    mesh_note = ""
    if USE_KPOINTS_OPT:
        # --- 主 KPOINTS：自洽网格 ---
        step2_kp = os.path.join(STEP2_DIR, "KPOINTS")
        use_auto = (SCF_MESH_SOURCE == "kpoints") and is_automatic_kpoints(step2_kp)
        if SCF_MESH_SOURCE == "kpoints" and not use_auto:
            print("[注意] step2 的 KPOINTS 不是自动网格格式，已回退到 IBZKPT 显式列表。"
                  "此时 step3/step4 的 ISYM 必须 >=1，否则电荷密度不会被对称化。",
                  file=sys.stderr)
        if use_auto:
            # VASP 在本步用【本步自己的 ISYM / SOC】重新约化，闭环一致，
            # 不存在"外部约化的列表 + 本步不做对称化"这种错配
            shutil.copyfile(step2_kp, kpoints_file)
            mesh_note = ("沿用 step2 的自动网格（VASP 本步自行按 ISYM 约化；"
                         "step2 的 IBZKPT 有 %d 个不可约点，可作预估）" % nuni)
        else:
            with open(kpoints_file, "w") as f:
                f.write("SCF mesh: %d uniform weighted k-points (path -> KPOINTS_OPT)\n" % nuni)
                f.write("%d\n" % nuni)
                f.write("Reciprocal\n")
                for kx, ky, kz, w in uni:
                    f.write("%+18.14f %+18.14f %+18.14f  %6d\n" % (kx, ky, kz, w))
            mesh_note = ("IBZKPT 显式加权列表（%d 点，来自 step2 的 ISYM=2 约化；"
                         "本步 ISYM 必须 >=1）" % nuni)
        # --- KPOINTS_OPT：全部路径点（自洽后 one-shot；权重不参与自洽，写 1）---
        with open(kopt_file, "w") as f:
            f.write("Band path: %d path + %d extra points [%s]\n" % (npath, nextra, ltype))
            f.write("%d\n" % (npath + nextra))
            f.write("Reciprocal\n")
            for p in path_pts:
                f.write("%+18.14f %+18.14f %+18.14f       1\n" % (p[0], p[1], p[2]))
            for _, p in extra:
                f.write("%+18.14f %+18.14f %+18.14f       1\n" % (p[0], p[1], p[2]))
    else:
        # --- 旧方案：均匀点 + 零权重路径点，全塞进 KPOINTS ---
        if os.path.exists(kopt_file):
            os.remove(kopt_file)          # 防止上一次生成的残留被 VASP 自动读走
        with open(kpoints_file, "w") as f:
            f.write("HSE band-dft-cpu: %d uniform(weighted) + %d path + %d extra (zero-weight) [%s]\n"
                    % (nuni, npath, nextra, ltype))
            f.write("%d\n" % (nuni + npath + nextra))
            f.write("Reciprocal\n")
            for kx, ky, kz, w in uni:
                f.write("%+18.14f %+18.14f %+18.14f  %6d\n" % (kx, ky, kz, w))
            for p in path_pts:
                f.write("%+18.14f %+18.14f %+18.14f       0\n" % (p[0], p[1], p[2]))
            for _, p in extra:
                f.write("%+18.14f %+18.14f %+18.14f       0\n" % (p[0], p[1], p[2]))

    # ---- kpath.json：画图脚本直接按【索引】取标签，不再靠坐标撞库 ----
    import json as _json
    point_labels = [None] * (npath + nextra)
    for li, lab in path_labels.items():
        point_labels[li] = _pretty(lab)
    for j, (lab, _f) in enumerate(extra):
        point_labels[npath + j] = _pretty(lab)
    kpath_meta = {
        "method": ltype,
        "note": info,
        "line_density": LINE_DENSITY,
        "labels": {_pretty(l): [float(x) for x in f] for l, f in kpt_coords.items()},
        "segments": [[_pretty(l) for l in seg] for seg in segments],
        "n_path": npath + nextra,
        "breaks": breaks,                       # 段间跳变发生在这些索引【之前】
        "kpoints": [[float(x) for x in p] for p in path_pts]
                   + [[float(x) for x in f] for _l, f in extra],
        "point_labels": point_labels,           # 与 kpoints 一一对应，无标签处为 null
        "lattice": [[float(x) for x in row] for row in lat],
    }
    Path(os.path.join(STEP3_DIR, "kpath.json")).write_text(
        _json.dumps(kpath_meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # 搭建 step3 其余文件
    setup_log, warn = [], []
    method = "unknown"
    magnetic, mag_note = False, "非磁"

    _poscar_txt = Path(struct).read_text(encoding="utf-8-sig")
    with open(os.path.join(STEP3_DIR, "POSCAR"), "w", encoding="utf-8", newline="\n") as _fh:
        _fh.write(_poscar_txt)
    setup_log.append("%s -> POSCAR" % struct)

    # POTCAR 从 step2 继承；submit.sh 也继承 step2（见下方，仅覆盖 Slurm 参数）
    src = os.path.join(STEP2_DIR, POTCAR_FILE)
    if os.path.exists(src):
        shutil.copyfile(src, os.path.join(STEP3_DIR, POTCAR_FILE))
        setup_log.append(POTCAR_FILE)
    else:
        warn.append("找不到 %s/%s" % (STEP2_DIR, POTCAR_FILE))

    method_src = os.path.join(STEP2_DIR, METHOD_FILE)
    dst_method = os.path.join(STEP3_DIR, METHOD_FILE)
    if os.path.exists(method_src):
        shutil.copyfile(method_src, dst_method)
        setup_log.append(METHOD_FILE)
    # 无论是否继承到，都确保 step3 的 method 文件带 DIM=，供 step4 继承
    _mtxt = Path(dst_method).read_text(encoding="utf-8") if os.path.exists(dst_method) else ""
    if "DIM=" not in _mtxt.upper():
        with open(dst_method, "a", encoding="utf-8") as _mf:
            _mf.write("DIM=%s\n" % dim.upper())

    # submit.sh：从 skill 模板渲染（按 SOC + 维度选），再覆盖 Slurm 三参数
    submit_params = dict(SUBMIT_DEFAULTS)
    if submit_params["JOBNAME"] is None:
        submit_params["JOBNAME"] = sanitize_label("%s_s3wave" % label)[:80]
    tpl_base = SUBMIT_TPL_NCL if soc else SUBMIT_TPL_STD
    submit_tpl = str(resolve_tpl(Path.cwd(), tpl_base, dim))
    submit_out = os.path.join(STEP3_DIR, "submit.sh")
    render_submit(submit_tpl, submit_out, submit_params)
    setup_log.append("submit.sh ← %s (%s, JOBNAME=%s)"
                     % (submit_tpl, "vasp_ncl" if soc else "vasp_std",
                        submit_params["JOBNAME"]))
    sub_ov = dict(SUBMIT_OVERRIDE)
    sub_ov.update(stepconf.read_submit(stepconf.CONF_NAME))
    _sub_changed = stepconf.apply_submit(submit_out, sub_ov)
    if _sub_changed:
        setup_log.append("   覆盖 Slurm: %s" % ", ".join(_sub_changed))

    incar_src = os.path.join(STEP2_DIR, INCAR_FILE)
    if os.path.exists(incar_src):
        items  = parse_incar(incar_src)
        method = detect_method(items, os.path.join(STEP2_DIR, METHOD_FILE))
        ispin  = 1
        for k, v in items:
            if k == "ISPIN":
                try:
                    ispin = int(v.split()[0])
                except ValueError:
                    pass
        nbands, nb_src = resolve_nbands(struct, ispin, STEP2_DIR, soc)
        incar_set    = dict(INCAR_SET)
        incar_remove = list(INCAR_REMOVE)
        tag = "%s+SOC" % method if soc else method
        incar_set["SYSTEM"] = "%s %s pre-converge WAVECAR (step3)" % (label, tag)

        # ---- ISYM ----
        _isym = "2" if STEP3_ISYM == "auto" else str(STEP3_ISYM).strip()
        if _isym in ("0", "-1") and not use_auto:
            sys.exit("[错误] STEP3_ISYM=%s（关对称）与 SCF_MESH_SOURCE='ibzkpt'"
                     "（外部约化的显式 k 点列表）不能并存。\n"
                     "        关对称必须配完整网格，否则电荷密度不会被对称化，rho 会算错。\n"
                     "        对策：把 SCF_MESH_SOURCE 改成 'kpoints'（自动网格，"
                     "VASP 会按本步 ISYM 自行生成相符的网格）。" % _isym)
        incar_set["ISYM"] = _isym

        # ---- ISMEAR / SIGMA ----
        _ism, _sig, _smnote = resolve_smearing(STEP2_DIR, items)
        incar_set["ISMEAR"], incar_set["SIGMA"] = _ism, _sig
        print("[..] 展宽：ISMEAR=%s SIGMA=%s —— %s" % (_ism, _sig, _smnote),
              file=sys.stderr)
        if USE_KPOINTS_OPT and KPOINTS_OPT_NKBATCH:
            incar_set["KPOINTS_OPT_NKBATCH"] = str(KPOINTS_OPT_NKBATCH)

        # ---- 磁性判定 + 初始磁矩来源 ----
        symbols, counts = read_species_and_counts(struct)
        nions        = sum(counts)
        moms_species = expand_per_species(MAGMOM_PER_SPECIES, symbols, counts)
        moms_outcar  = read_magnetization(os.path.join(STEP2_DIR, OUTCAR_FILE), nions)
        moms_defaults = default_moments(symbols, counts)
        outcar_max   = max((abs(m) for m in moms_outcar), default=0.0) if moms_outcar else 0.0

        moments, msrc = None, "—"
        if MAGNETIC == "auto":
            if moms_species is not None:
                magnetic, moments, msrc = True, moms_species, "MAGMOM_PER_SPECIES(手动)"
            elif ispin == 2 and moms_outcar is not None:
                if outcar_max > MAG_ZERO_TOL:
                    magnetic, moments, msrc = True, moms_outcar, "step2 OUTCAR 收敛磁矩"
                else:
                    magnetic = False
                    setup_log.append("step2 收敛磁矩全部≈0(max|m|=%.3f) —— 自动按非磁处理"
                                     % outcar_max)
            elif ispin == 2:
                magnetic = True     # step2 是磁性但读不到磁矩 -> 下面走告警回退
            elif moms_defaults is not None:
                magnetic, moments, msrc = True, moms_defaults, "元素默认高自旋起点"
                warn.append("POSCAR 含磁性候选元素但 step2 是非磁跑的！本步已自动改为磁性"
                            "（元素高自旋起点）。注意几何来自非磁弛豫，磁性体系建议"
                            "用新版 gen_step1/2 从头重跑。")
            else:
                magnetic = False
        else:
            magnetic = bool(MAGNETIC)
            if magnetic:
                if moms_species is not None:
                    moments, msrc = moms_species, "MAGMOM_PER_SPECIES(手动)"
                elif moms_outcar is not None and outcar_max > MAG_ZERO_TOL:
                    moments, msrc = moms_outcar, "step2 OUTCAR 收敛磁矩"
                elif moms_defaults is not None:
                    moments, msrc = moms_defaults, "元素默认高自旋起点"

        if magnetic and moments is None:
            warn.append("判定为磁性但拿不到初始磁矩：请设 MAGMOM_PER_SPECIES，或把 "
                        "step1/2 以 ISPIN=2 磁性重跑，让 step3 继承其收敛磁矩。"
                        "本次回退为非磁起点。")

        mag_ok = magnetic and moments is not None
        magmom_str, ispin_set, mag_tags, mag_note = build_magmom(
            soc, mag_ok, moments, nions, SAXIS)

        if soc:
            incar_set["LSORBIT"]    = ".TRUE."
            incar_set["GGA_COMPAT"] = ".FALSE."   # 非共线下恢复 xc 的正确对称
            incar_set["LMAXMIX"]    = "4"          # d/_d(半芯)赝势 + 混合需要
            # ⚠️ vasp_ncl 需要 NCORE=1，否则初始化阶段 FPE。
            #    但 KPOINTS_OPT 下 VASP 官方明确表示"不能与 NPAR/NCORE 标签同时使用"
            #    （即便值是 1 也不要写），而不写 NCORE 时的默认值本来就是 1，
            #    两个要求正好相容 —— 所以这里什么都不写。
            if not USE_KPOINTS_OPT:
                incar_set["NCORE"] = SOC_NCORE
            incar_remove.append("ISPIN")           # 非共线不用 ISPIN；step4 也拒收 ISPIN=2
            setup_log.append("SOC: LSORBIT=.TRUE., NBANDS 已×2, NCORE=1（%s）, 已剔除 ISPIN"
                             % ("默认值，KPOINTS_OPT 下不写该标签" if USE_KPOINTS_OPT
                                else "显式写入"))

        if magmom_str is not None:
            incar_set["MAGMOM"] = magmom_str
        else:
            incar_remove.append("MAGMOM")          # 非磁非 SOC：不写 MAGMOM
        if ispin_set is not None:
            incar_set["ISPIN"] = ispin_set
        for k, v in mag_tags.items():
            incar_set[k] = v
        setup_log.append("磁性: %s%s" % (mag_note, "（来源: %s）" % msrc if mag_ok else ""))

        # ---- 并行：KPOINTS_OPT 下只能用 KPAR（NCORE 恒为 1，不写 NPAR/NCORE）----
        kpar, npar_grp, nbands_adj, par_note = auto_parallel(STEP3_DIR, nuni, nbands)
        if kpar:
            if PARALLEL_MODE == "auto":
                incar_set["KPAR"] = str(kpar)
            if nbands and nbands_adj != nbands:
                setup_log.append("NBANDS %d -> %d（对齐组内 NPAR=%d）"
                                 % (nbands, nbands_adj, npar_grp))
                nbands = nbands_adj
            setup_log.append("并行: KPAR=%d，组内 NPAR=%d，NCORE=1（不写 NPAR/NCORE）  [%s]"
                             % (kpar, npar_grp, par_note))
            if nuni and kpar > nuni:
                warn.append("KPAR=%d 大于自洽不可约 k 点数 %d，自洽阶段会有组闲着" % (kpar, nuni))
        else:
            warn.append("未能自动确定 KPAR（%s）—— 请手动在 INCAR 里加 KPAR" % par_note)

        if nbands is not None:
            incar_set["NBANDS"] = str(nbands)
            setup_log.append("NBANDS = %d  (%s)" % (nbands, nb_src))
        else:
            warn.append("NBANDS 无法自动确定（%s），请手动设置 STEP3_NBANDS" % nb_src)
        text = build_step3_incar(items, incar_remove, incar_set)
        with open(os.path.join(STEP3_DIR, "INCAR"), "w") as f:
            f.write(text)
        keys2   = {k for k, _ in items}
        removed = [k for k in incar_remove if k.upper() in keys2]
        overr   = [k for k in incar_set    if k.upper() in keys2]
        added   = [k for k in incar_set    if k.upper() not in keys2]
        setup_log.append("INCAR (删:%s | 改:%s | 增:%s)"
                         % (",".join(removed) or "无",
                            ",".join(overr)   or "无",
                            ",".join(added)   or "无"))
    else:
        warn.append("找不到 %s/INCAR" % STEP2_DIR)

    # 摘要
    print("结构文件      : %s" % struct)
    print("k 路径        : %s\n                %s" % (ltype, info))
    print("方法          : %s（从 step2 继承）" % method)
    print("SOC           : %s   [%s]" % (
        "ON — 产出非共线 WAVECAR (vasp_ncl)" if soc else "off (vasp_std)", soc_note))
    print("磁性          : %s" % ("ON — %s" % mag_note if magnetic else "off (非磁)"))
    print("自洽网格      : %s" % (mesh_note or ("%d 点 ← %s" % (nuni, ibzkpt))))
    print("路径点(权重0) : %d" % npath)
    if nextra:
        print("额外点(权重0) : %d   (%s)" % (nextra, ", ".join(l for l, _ in extra)))
    print("路径          : %s" % " | ".join(route))
    print("高对称点      : %s" % ", ".join(_pretty(l) for l in sorted(kpt_coords)))
    if seglens_main:
        print("首段各段长度(Ang^-1):")
        for l0, l1, d in seglens_main:
            print("   %s-%s : %.4f" % (_pretty(l0), _pretty(l1), d))
    if USE_KPOINTS_OPT:
        print("方案          : KPOINTS_OPT（自洽只用 %d 个均匀点；%d 个路径点自洽后 one-shot）"
              % (nuni, npath + nextra))
        print("-> 已写出 %s（%d 点，自洽用）" % (kpoints_file, nuni))
        print("-> 已写出 %s（%d 点，one-shot 用；本征值写进 vasprun.xml)"
              % (kopt_file, npath + nextra))
    else:
        print("方案          : 零权重（旧）——路径点在每个电子步都参与，HSE 下非常贵")
        print("-> 已写出 %s（共 %d 点）" % (kpoints_file, nuni + npath + nextra))
    print("\n已搭建 %s:" % STEP3_DIR)
    for item in setup_log:
        print("   + %s" % item)
    if warn:
        print("\n[注意]")
        for w in warn:
            print("   ! %s" % w)
    if USE_KPOINTS_OPT:
        print("\n路径点在 EIGENVAL_OPT 里的序号（1-based，只含路径点，没有均匀网格点）:")
        for local_idx, lab in sorted(path_labels.items()):
            print("   %-3s  第 %d 号 k 点" % (lab, local_idx + 1))
        for j, (lab, _) in enumerate(extra):
            print("   %-3s  第 %d 号 k 点" % (lab, npath + j + 1))
    else:
        print("\nEIGENVAL 里高对称点位置（1-based 全局 k 序号，均匀网格点排在前）:")
        for local_idx, lab in sorted(path_labels.items()):
            print("   %-3s  第 %d 号 k 点" % (lab, nuni + local_idx + 1))
        for j, (lab, _) in enumerate(extra):
            print("   %-3s  第 %d 号 k 点" % (lab, nuni + npath + j + 1))
    print("\n下一步:")
    if USE_KPOINTS_OPT:
        print("   0. 确认超算上的 VASP >= 6.3（KPOINTS_OPT 是 6.3 才引入的）:")
        print("      grep -m1 'vasp\\.6' %s/OUTCAR   # 或直接看 vasp_ncl 的 banner" % STEP3_DIR)
        print("      若是 5.4.x，请把脚本顶部 USE_KPOINTS_OPT 改回 False。")
    print("   1. 检查 %s/submit.sh（应调用 %s）后提交"
          % (STEP3_DIR, "vasp_ncl" if soc else "vasp_std"))
    print("   2. 跑完后在父目录运行 gen_step4_HSE.py 搭 step4")
    if USE_KPOINTS_OPT:
        print("   3. 跑完后确认 %s/vasprun.xml 里有 <eigenvalues_kpoints_opt>（%d 个 k 点）；"
              "grep -c eigenvalues_kpoints_opt vasprun.xml" % (STEP3_DIR, npath + nextra))


if __name__ == "__main__":
    main()
