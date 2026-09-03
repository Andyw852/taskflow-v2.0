# =====================================================================
# incar_2d.tpl —— mlff-mace step1：2D 材料紧弛豫模板（三段式 a/b/c）
#
# 与 3D 版的差别：变胞约束（IOPTCELL）锁死真空方向（c 轴）及含 c 的剪切。
# relax_common 按 CELL_CONSTRAINT_2D='ioptcell_tag' 原样保留下面这行 IOPTCELL。
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
# ---- 2D 变胞约束（IOPTCELL 3x3 展平）：面内 a/b 的 xx/yy/xy 放开，含 c 的分量锁死 ----
# 缺这行的话 ISIF=3 会连真空一起弛豫，c 轴漂移、能量发散（qHPC36 实测）。
IOPTCELL = 1 1 0 1 1 0 0 0 0

# ---- 输出 ----
LWAVE  = .FALSE.
LCHARG = .FALSE.

# ---- 并行（12 核：KPAR=1、NCORE=4）----
NCORE  = 4
KPAR   = 1

# ---- 2D 专属提示 ----
# 真空方向胞长被锁死（IOPTCELL），external pressure 的真空分量不为零是正常的；
# 判据只查面内 XX/YY 分量 ≤ 2 kB（checks.py ck_step1）。
