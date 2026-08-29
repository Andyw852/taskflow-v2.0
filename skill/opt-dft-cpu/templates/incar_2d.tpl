# =====================================================================
# incar_2d.tpl —— 2D 体系结构优化模板
#
# 【本模板只完全决定 step1】
#   step2~4 会强制改写 EDIFF / IBRION / NSW / ISYM / ISMEAR / SIGMA /
#   LWAVE / LCHARG / NCORE / KPAR，并删除 EDIFFG / POTIM / ISIF / IOPTCELL。
#   要改那几项请改对应的 gen_stepN 脚本顶部配置，改这里不生效。
#
# 【opt-dft-cpu 分段弛豫】调度写死在 gen_step1_opt.py（不是公共池默认的三段）：
#   先固定胞安顿原子、再放开胞，力判据统一 -0.05；EARLY_EXIT 关（否则会跳过放开胞段）：
#     阶段 a  ISIF=2 IBRION=2(CG)  EDIFFG=-0.05  NSW=80   （固定胞，去 IOPTCELL）
#     阶段 b  ISIF=3 IBRION=2(CG)  EDIFFG=-0.05  NSW=120  （放开胞，保留面内 IOPTCELL）
#   解耦自由度是为提速：生成结构离极小值远，单段变胞硬啃会白走很多离子步。
#   下面写的 ISIF/IBRION/POTIM/EDIFFG/NSW 只在 single 模式生效。
#
# 【占位符】gen_step1 自动填充：{{SYSTEM}} {{ENCUT}} {{GGA}} {{VDW_LINE}}
# 【自动注入，别在这写】ISPIN / MAGMOM / NUPDOWN / LMAXMIX / LDAU*
# =====================================================================

SYSTEM = {{SYSTEM}}

# ---- 起始 / 泛函 ----
ISTART = 0
ICHARG = 2
GGA    = {{GGA}}
{{VDW_LINE}}

# ---- 精度 ----
PREC    = Accurate
ENCUT   = {{ENCUT}}       # = ceil(1.5 x max ENMAX)，由 gen_step1 的 ENCUT_FACTOR 决定
                          # step1~4 必须一致，否则总能不在同一基组上，能量差全错
LREAL   = Auto           # 大超胞实空间投影，粗弛豫更快（VASP log 亦建议）；S2 静态用 .FALSE. 出精确总能
LASPH   = .TRUE.
ADDGRID = .FALSE.         # VASP6 官方不推荐开启，PREC=Accurate 已足够

# ---- 电子步 ----
ALGO   = Normal
EDIFF  = 1E-4
NELM   = 200              # 配合 1E-7；电子步没收敛 VASP 不会停，会拿错的力继续走
NELMIN = 6
AMIN   = 0.01             # 2D 长真空层必加，否则电子步收敛极慢甚至震荡
ISMEAR = 0
SIGMA  = 0.05
# 金属性 2D 改为：ISMEAR = 1 / SIGMA = 0.2

# ---- 离子步（三段式下会被 gen_step1 覆盖）----
IBRION = 2
ISIF   = 3
ISYM   = 2
POTIM  = 0.2
NSW    = 40
EDIFFG = -0.05
IOPTCELL = 1 1 0 1 1 0 0 0 0    # 面内 xx/yy/xy 放开，c 冻结；仅 ISIF>=3 生效

# ---- 输出 ----
LWAVE  = .FALSE.
LCHARG = .TRUE.
LORBIT = 11

# ---- 并行 ----
NCORE  = 6                # (总核数 / KPAR) 须能被 NCORE 整除；GPU(OpenACC) 版必须为 1
KPAR   = 2

# ---- 非对称 2D（Janus / 单面吸附）按需开 ----
# LDIPOL = .TRUE.
# IDIPOL = 3
# DIPOL  = 0.5 0.5 0.5
# ISYM   = 0              # 开偶极修正后 step1 要用 0；step2~4 请改脚本里的 STEP3_ISYM/STEP4_ISYM
