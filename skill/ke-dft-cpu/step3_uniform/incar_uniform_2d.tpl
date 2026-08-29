# =====================================================================
# incar_uniform_2d.tpl —— AMSET uniform 密网格自洽（2D）
# 产出 WAVECAR + vasprun.xml 供 amset wave。占位符：{{SYSTEM}} {{ENCUT}} {{GGA}}
# 【可改】NEDOS/ISMEAR/SIGMA/KPAR/NCORE 在生成后的 INCAR 里直接调。
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

ALGO   = Normal
EDIFF  = 1E-8            # AMSET 要求高精度波函数
NELM   = 200
NELMIN = 6
AMIN   = 0.01          # 2D 长真空层电子步稳定
ISMEAR = 0              # AMSET 一律高斯小展宽，别用 -5
SIGMA  = 0.01

IBRION = -1
NSW    = 0
ISYM   = 2

# ---- AMSET 关键：输出致密 DOS 与波函数 ----
LWAVE  = .TRUE.         # amset wave 读 WAVECAR
LCHARG = .FALSE.
LORBIT = 11
NEDOS  = 5000
LOPTICS = .FALSE.       # amset 自己算跃迁，不需要 VASP 的 LOPTICS

NCORE  = 6
KPAR   = 2
