# =====================================================================
# incar_deform_3d.tpl —— 形变势单点（3D）。每个 deform-NN / undeformed 一份。
# 固定结构单点，收紧 EDIFF 取精确带边能量。占位符：{{SYSTEM}} {{ENCUT}} {{GGA}}
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
EDIFF  = 1E-8          # 带边能量差，收紧（AMSET 官方形变势设置）
ADDGRID= .TRUE.        # AMSET 官方形变势设置
                       # ★不要设 ICORELEVEL=1★：amset deform read 首选参考是
                       #   OUTCAR 的「各原子核处平均静电势」（"the norm of the test
                       #   charge is" 块）；ICORELEVEL=1 会让 VASP 不输出该块，
                       #   迫使 amset 走 1s 芯能级 fallback（元素依赖、非刚性，
                       #   会把 E1p 压到 ~0.3 eV 量级）。留默认即输出平均芯势。
NELM   = 200
NELMIN = 6
ISMEAR = 0
SIGMA  = 0.05

IBRION = -1            # 单点，不动离子
NSW    = 0
ISIF   = 2
ISYM   = 0             # 形变构型对称性更低；ISYM>=1 时各目录
                       #   IBZ 不同，逐 k 差分会对不上

LWAVE  = .FALSE.
LCHARG = .FALSE.
LORBIT = 0
LVHAR  = .TRUE.         # 输出 LOCPOT（2D 真空对齐用；3D 无真空，仅保持一致性）

NCORE  = 6
KPAR   = 4
