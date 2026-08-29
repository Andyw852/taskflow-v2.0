# =====================================================================
# incar_2d.tpl —— mlff-mace step1：2D 材料紧弛豫模板（三段式 a/b/c）
#
# 与 3D 版的差别：变胞约束（IOPTCELL）由 relax_common 按 CELL_CONSTRAINT_2D
# 注入（真空方向胞长与含真空方向的剪切锁死），本模板不写 IOPTCELL。
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

# ---- 电子步 ----
ALGO   = Normal
EDIFF  = 1E-7
NELM   = 200
NELMIN = 6
ISMEAR = 0
SIGMA  = 0.05
# 金属性 2D 改成：ISMEAR = 1 / SIGMA = 0.2

# ---- 离子步（三段式下被 gen_step1 覆盖为 EDIFFG=-0.001 / NSW 300/300/200）----
IBRION = 2
ISIF   = 3
ISYM   = 2
POTIM  = 0.2
NSW    = 200
EDIFFG = -0.001

# ---- 输出 ----
LWAVE  = .FALSE.
LCHARG = .FALSE.

# ---- 并行（12 核：KPAR=1、NCORE=4）----
NCORE  = 4
KPAR   = 1

# ---- 2D 专属提示 ----
# 真空方向胞长被锁死（IOPTCELL），external pressure 的真空分量不为零是正常的；
# 判据只查面内 XX/YY 分量 ≤ 2 kB（checks.py ck_step1）。
