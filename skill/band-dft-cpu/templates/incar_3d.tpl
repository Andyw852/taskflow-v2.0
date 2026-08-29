# =====================================================================
# incar_3d.tpl —— 3D 体相结构优化模板
#
# 【本模板只完全决定 step1】
#   step2~4 会强制改写 EDIFF / IBRION / NSW / ISYM / ISMEAR / SIGMA /
#   LWAVE / LCHARG / NCORE / KPAR，并删除 EDIFFG / POTIM / ISIF / IOPTCELL。
#   要改那几项请改对应的 gen_stepN 脚本顶部配置，改这里不生效。
#
# 【三段式弛豫】gen_step1 的 RELAX_STAGES="auto" 会按阶段覆盖下面这几项：
#     阶段 a  ISIF=2 IBRION=2 EDIFFG=-0.02  NSW=200  （固定胞）
#     阶段 b  ISIF=3 IBRION=2 EDIFFG=-0.01  NSW=200  
#     阶段 c  ISIF=3 IBRION=1 EDIFFG=-0.001 NSW=100  
#   所以下面写的 ISIF/IBRION/POTIM/EDIFFG/NSW 只在 RELAX_STAGES="single" 时生效。
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
LREAL   = .FALSE.
LASPH   = .TRUE.
ADDGRID = .FALSE.         # VASP6 官方不推荐开启，PREC=Accurate 已足够

# ---- 电子步 ----
ALGO   = Normal
EDIFF  = 1E-7
NELM   = 200              # 配合 1E-7；电子步没收敛 VASP 不会停，会拿错的力继续走
NELMIN = 6
# AMIN = 0.01             # 只有长晶格矢量(2D/表面)才需要，体相不用
ISMEAR = 0
SIGMA  = 0.05
# 金属性 2D 改为：ISMEAR = 1 / SIGMA = 0.2

# ---- 离子步（三段式下会被 gen_step1 覆盖）----
IBRION = 2
ISIF   = 3
ISYM   = 2
POTIM  = 0.2
NSW    = 40
EDIFFG = -0.01

# ---- 输出 ----
LWAVE  = .FALSE.
LCHARG = .TRUE.
LORBIT = 11

# ---- 并行 ----
NCORE  = 6                # (总核数 / KPAR) 须能被 NCORE 整除；GPU(OpenACC) 版必须为 1
KPAR   = 4

# ---- 3D 专属提示 ----
# ISIF=3 全胞放开后，external pressure 应收敛到 0 附近（2D 冻结 c 轴时做不到）：
#     grep "external pressure" OUTCAR | tail -1     # |p| < 0.5 kB 才算真收敛
# EDIFFG 负值只判力、不判应力，所以还要 cp CONTCAR POSCAR 重跑到晶格常数不再变。
