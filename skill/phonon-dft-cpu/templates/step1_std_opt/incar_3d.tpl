# =====================================================================
# incar_3d.tpl —— step1 标准化后几何优化（弹性流程）(3D 体相,ISIF=3 全胞弛豫)
#
# 占位符(gen_step1 填充,双大括号包住键名):
#   SYSTEM    来自 POSCAR 标题/化学式
#   ENCUT     ceil(1.5 x max ENMAX in POTCAR),或 MANUAL_ENCUT
#   GGA       由顶部 FUNC 决定(pbe / pbe-d3 -> PE,pbesol -> PS)
#   VDW_LINE  pbe-d3 -> "IVDW = 12",其余 -> 注释行
# 想手动固定某项,直接把占位符替换成数值(脚本只填模板里实际出现的)。
# 其余参数可自由增删改,脚本原样透传;step2/3/4 从上一步继承并按各自规则改写。
#
# 【自动注入,不要在本模板里写】ISPIN / MAGMOM / NUPDOWN / LMAXMIX / LDAU*
#   写了会被静默覆盖;要改请改 gen_step1 顶部配置,或改生成后的 stepN/INCAR。
#
# 【ISIF=3 必读】EDIFFG 负值只判断力、不判断应力,单轮结果必然带 Pulay 应力。
#   收敛后须 cp CONTCAR POSCAR 重跑,直到晶格常数变化 < 0.001 Å(通常 2~3 轮)。
# =====================================================================

SYSTEM = {{SYSTEM}}

# ---- 起始 / 泛函 ----
ISTART = 0
ICHARG = 2
GGA    = {{GGA}}
{{VDW_LINE}}
# GGA_COMPAT = .FALSE.    # 可选:低对称/强各向异性胞,恢复梯度项旋转不变性

# ---- 精度 ----
PREC    = Accurate
ENCUT   = {{ENCUT}}       # 全流程 step1~4 必须一致,否则能量不可比
LREAL   = .FALSE.
LASPH   = .TRUE.

# ---- 电子步 ----
ALGO   = Normal
EDIFF  = 1E-7        # 弹性：收紧到 1E-7 服务近零应力基态
NELM   = 100
NELMIN = 6
ISMEAR = 0                # 半导体/绝缘体/未知体系
SIGMA  = 0.05
# 金属改为: ISMEAR = 1 / SIGMA = 0.2,并检查 OUTCAR 的 "entropy T*S" < 1 meV/atom
# 弛豫阶段不要用 ISMEAR = -5(四面体法的力和应力不可靠),留给 step3/4 的 DOS

# ---- 离子步 ----
IBRION = 2
ISIF   = 3
ISYM   = 2                # 保持空间群;若要搜索对称性破缺相(畸变/极化)改 0
POTIM  = 0.3
NSW    = 300
EDIFFG = -0.001      # 弹性：力判据收紧到 5 meV/Å

# ---- 输出 ----
LWAVE  = .FALSE.          # 多轮 Pulay 弛豫中晶胞在变,WAVECAR 无法跨轮复用
LCHARG = .TRUE.

# ---- 并行 ----
NCORE  = 6                # (总核数 / KPAR) 须能被 NCORE 整除
KPAR   = 4                # 内存约 ×4;GPU(OpenACC)版必须 NCORE = 1


# =====================================================================
# 附:参数速查(仅注释)
# =====================================================================
# ---- IBRION ----
# -1 静态(配 NSW=0) | 0 MD | 1 RMM-DIIS(收尾快) | 2 CG(首选,稳健)
#  3 阻尼MD(救命) | 5/6 有限差分声子 | 7/8 DFPT
#
# ---- ISIF ----
#  2 只弛豫原子 | 3 原子+形状+体积(本模板) | 4 原子+形状,定容
#  6 形状+体积,原子不动 | 7 只变体积(状态方程/体模量)
#
# ---- ISMEAR ----
#  0 高斯,SIGMA=0.05 —— 半导体/绝缘体,通用安全选项
#  1 MP一阶,SIGMA=0.2 —— 金属弛豫
# -5 四面体+Blöchl —— 精确总能与 DOS,需 k 点 >= 4,不可用于弛豫
#
# ---- EDIFFG ----
# 负值 = 力判据(eV/Å):常规 -0.01,声子/高精度前 -0.001
# 正值 = 能量差判据,一般不用
#
# ---- ISYM ----
#  2 默认(PBE/PBEsol/SCAN 弛豫、SCF、能带、DOS;共线自旋也用它)
#  3 只对称化力和应力 —— 杂化泛函 LHFCALC=.T. 的默认值
#  0 关空间群 —— MD / EFIELD / LDIPOL / LCALCPOL / SOC能带 / 需破缺对称性
# -1 全关 —— 磁各向异性能 MAE(各 SAXIS 须共用同一 k 网格) / Wannier / 拓扑
# 注:关对称后 IBZ 不再约化,k 点数可能翻数倍,能不关就别关