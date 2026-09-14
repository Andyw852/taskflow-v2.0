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
# LPEAD：.TRUE. = 用有限差分(PEAD)求 |∇k u_nk>；.FALSE. = 解 Sternheimer 方程（纯 DFPT）。
# 两者都合法，官方 SiC 介电教程用的就是 LEPSILON=.TRUE.+LPEAD=.TRUE.+IBRION=8，
# 并指出 .TRUE. 对 k 点收敛往往更快 —— 所以这里**不是**"路线接错"，别据此禁用。
# 但官方同时声明：**LPEAD 不支持金属**。PEAD 的重叠矩阵 S 定义在**占据流形**上，
# 占据/空态划分一模糊，矩阵就退化。窄隙 / 强 SOC / 近金属体系正落在这个风险里。
# 实测（A2B2Te5 四体系, PBE+SOC, 带隙 0.15~0.23 eV, ISYM=2, KPAR=8）：
#   LPEAD=.TRUE.  -> IBRIOR=8 的 DFPT 直接 SIGSEGV（rc=139），死在 MACROSCOPIC 张量之前；
#   LPEAD=.FALSE. -> 正常产出 ε∞（含局域场）与 Born 有效电荷、离子贡献。
# 故默认取保守的 .FALSE.。体系明确是宽隙绝缘体时，可以自行开回 .TRUE. 换取更快的 k 收敛。
LPEAD    = .FALSE.
NSW      = 1
ISYM     = 2

LWAVE  = .FALSE.
LCHARG = .FALSE.

# ★DFPT 不能 k 点并行
NCORE  = 1
KPAR   = 1
