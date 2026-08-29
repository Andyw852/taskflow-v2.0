# =====================================================================
# incar_3d.tpl —— mlff-mace step1：3D 体相紧弛豫模板（三段式 a/b/c）
#
# 占位符：{{SYSTEM}} {{ENCUT}} {{GGA}} {{VDW_LINE}}（gen_step1 自动填充）
# 自动注入，别在这写：ISPIN / MAGMOM / NUPDOWN / LMAXMIX / LDAU*
# 三段式下 ISIF/IBRION/POTIM/EDIFFG/NSW 会被 gen_step1 覆盖（EDIFFG=-0.001）
# =====================================================================

SYSTEM = {{SYSTEM}}

# ---- 起始 / 泛函 ----
ISTART = 0
ICHARG = 2
GGA    = {{GGA}}
{{VDW_LINE}}

# ---- 精度（弛豫与后续单点同一基组，ENCUT 全程一致）----
PREC    = Accurate
ENCUT   = {{ENCUT}}
LREAL   = .FALSE.
LASPH   = .TRUE.
ADDGRID = .FALSE.

# ---- 电子步（紧弛豫：EDIFF 必须足够小，力才干净）----
ALGO   = Normal
EDIFF  = 1E-7
NELM   = 200
NELMIN = 6
ISMEAR = 0
SIGMA  = 0.05
# 金属体系把上面两行改成：ISMEAR = 1 / SIGMA = 0.2

# ---- 离子步（三段式下被 gen_step1 覆盖为 EDIFFG=-0.001 / NSW 300/300/200）----
IBRION = 2
ISIF   = 3
ISYM   = 2
POTIM  = 0.2
NSW    = 200
EDIFFG = -0.001

# ---- 输出（要 EIGENVAL 定带隙；不要波函数/电荷）----
LWAVE  = .FALSE.
LCHARG = .FALSE.

# ---- 并行（12 核：KPAR=1、NCORE=4；改核数用 tf conf --set）----
NCORE  = 4
KPAR   = 1

# ---- 3D 专属提示 ----
# 收敛判据（checks.py ck_step1）：力收敛 + 末次 external pressure ≤ 2 kB
#     grep "external pressure" OUTCAR | tail -1
