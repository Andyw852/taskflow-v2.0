# =====================================================================
# incar_dfpt_3d.tpl —— DFPT 介电常数（2D，IBRION=8 + LEPSILON）
# 一次微扰求 ε∞ 和 ε₀（离子+电子）。占位符：{{SYSTEM}} {{ENCUT}} {{GGA}}
# 【注意】DFPT 与 k 点并行不兼容 → 必须 KPAR=1；NCORE 也必须 =1。
# =====================================================================
SYSTEM = {{SYSTEM}}

ISTART = 0
ICHARG = 2
GGA    = {{GGA}}
{{VDW_LINE}}

PREC   = Accurate
ENCUT  = {{ENCUT}}
LREAL  = .FALSE.
LASPH  = .TRUE.
ADDGRID= .TRUE.        # patch_addgrid：细化增广网格，减小力常数/声学求和规则数值误差（amset 推荐）

ALGO   = Normal
EDIFF  = 1E-8
NELM   = 200
NELMIN = 6
AMIN   = 0.01          # 2D 长真空层电子步稳定
ISMEAR = 0
SIGMA  = 0.05

# ---- DFPT 微扰 ----
IBRION   = 8           # DFPT + 对称约化
LEPSILON = .TRUE.      # 静态介电张量（含离子贡献）
LPEAD    = .TRUE.      # 用 PEAD 求 ∂ψ/∂k，比有限差分稳
NSW      = 1
ISYM     = 2

LWAVE  = .FALSE.
LCHARG = .FALSE.

# ★DFPT 不能 k 点并行
NCORE  = 1
KPAR   = 1
