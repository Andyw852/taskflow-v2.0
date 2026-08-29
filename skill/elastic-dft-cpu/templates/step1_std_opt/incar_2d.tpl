# =====================================================================
# incar_2d.tpl —— step1 标准化后几何优化（弹性流程）(2D 体系,面内全弛豫 + c 轴固定)
#
# 占位符(gen_step1 填充): {{SYSTEM}} {{ENCUT}} {{GGA}} {{VDW_LINE}}
#
# 变胞约束流派(gen_step1 顶部 CELL_CONSTRAINT_2D 选择):
#   "optcell_file"  脚本把 IOPTCELL 行转成 OPTCELL 文件并删除该行
#                   (submit_std_2d.tpl 的 vasp.6.4.3-optcell 属此流派)
#   "ioptcell_tag"  原样保留 IOPTCELL 标签(需 VASP 认识该标签)
#   无补丁          删掉 IOPTCELL 并把 ISIF 改为 2,面内晶格另用能量-面积扫描定
#
# 【自动注入,不要在本模板里写】ISPIN / MAGMOM / NUPDOWN / LMAXMIX / LDAU*
#   写了会被静默覆盖;要改请改 gen_step1 顶部配置,或改生成后的 stepN/INCAR。
# =====================================================================

SYSTEM = {{SYSTEM}}

# ---- 起始 / 泛函 ----
ISTART = 0
ICHARG = 2
GGA    = {{GGA}}
{{VDW_LINE}}
# GGA_COMPAT = .FALSE.    # 可选:低对称/各向异性胞,恢复梯度项旋转不变性

# ---- 精度(ISIF=3 必须收紧)----
PREC    = Accurate
ENCUT   = {{ENCUT}}       # 应为 POTCAR 最大 ENMAX 的 ~1.3 倍
LREAL   = .FALSE.
LASPH   = .TRUE.
ADDGRID = .FALSE.

# ---- 电子步 ----
ALGO   = Normal
EDIFF  = 1E-7             # 服务于 EDIFFG=-0.005 的收紧力判据;非弹性用途可放宽到 1E-6
NELM   = 200
NELMIN = 6
AMIN   = 0.01             # 2D 长真空层必加,否则电子步收敛极慢
ISMEAR = 0                # 半导体/绝缘体
SIGMA  = 0.05
# 金属性 2D 改为: ISMEAR = 1 / SIGMA = 0.1~0.2

# ---- 离子步 ----
IBRION = 2
ISIF   = 3
ISYM   = 2
POTIM  = 0.2
NSW    = 300
EDIFFG = -0.005      # 弹性：力判据收紧到 5 meV/Å
IOPTCELL = 1 1 0 1 1 0 0 0 0    # 面内 xx/yy/xy 放开,c 冻结;仅 ISIF>=3 生效

# ---- 输出 ----
LWAVE  = .FALSE.
LCHARG = .TRUE.

# ---- 并行 ----
NCORE  = 6                # 总核数/KPAR 须被 NCORE 整除;GPU(OpenACC)版必须 NCORE=1
KPAR   = 2                # 内存 ×2,2D 大真空胞注意 OOM

# ---- 非对称 2D(Janus / 单面吸附)按需开 ----
# LDIPOL = .TRUE.
# IDIPOL = 3
# DIPOL  = 0.5 0.5 0.5    # 电荷质心分数坐标
# ISYM   = 0              # 开偶极修正后必须改 0


# =====================================================================
# 附:参数速查(仅注释,不影响运行)
# =====================================================================
# ---- IBRION ----
# -1  离子不动 —— 静态计算(SCF/DOS/能带/电荷/光学),必须配 NSW = 0
#  0  分子动力学 —— 配 SMASS / TEBEG / TEEND / POTIM(时间步,fs)
#  1  准牛顿 RMM-DIIS —— 接近极小值时收尾,快;初始结构差会跑飞
#  2  共轭梯度 CG —— 结构优化首选,最稳健
#  3  阻尼 MD —— CG/DIIS 都不收敛时的救命选项,配 SMASS
#  5  有限差分 Hessian —— 分子/团簇振动频率(不做对称约化)
#  6  有限差分 + 对称约化 —— 声子(超胞法)、拉曼、零点能,配 NFREE/POTIM
#  7  DFPT —— 力常数/介电,仅 Γ 点,部分泛函不支持
#  8  DFPT + 对称约化 —— 声子/Born 有效电荷,配 LEPSILON = .TRUE.
#
# ---- ISIF ----
#  2  只弛豫原子 —— 表面、吸附、缺陷、分子
#  3  原子 + 晶胞形状 + 体积 —— 须提高 ENCUT 30% 并做 2~3 轮
#  4  原子 + 形状,体积固定
#  6  形状 + 体积,原子固定
#  7  只变体积 —— 状态方程 / 弹性
#
# ---- EDIFFG ----
# 负值 = 力判据(eV/Å),常规 -0.02,声子前 -0.001
# 正值 = 能量差判据,一般不用
# ！负值只判断力,不判断应力 —— ISIF=3 必须另行检查 stress tensor
#
# ---- ISYM ----
#  2  默认。PBE/PBEsol/SCAN 的弛豫、SCF、能带、DOS;共线自旋(ISPIN=2)也用它
#  3  只对称化力和应力。杂化泛函(LHFCALC=.T.)默认值,HSE06/PBE0 保持即可
#  0  关空间群对称(保留时间反演):
#       分子动力学 / EFIELD / LDIPOL+IDIPOL / LCALCPOL
#       非共线+SOC(LSORBIT=.T.)的 SCF 与能带 —— 稳妥起见
#       需要打破人为高对称性(Jahn-Teller、极化位移、缺陷)
# -1  全关(连时间反演):
#       磁各向异性能 MAE —— 不同 SAXIS 必须共用同一 k 网格!
#       Wannier90 / 拓扑 —— 需完整 BZ 波函数
#       对称性报错的终极排查
#
# 注1:关对称后 IBZ 不再约化,k 点数可能翻数倍,能不关就别关
# 注2:SOC 流程 = 共线弛豫(2) → 共线SCF(2) → 非共线SCF(0) → 能带(0)
# 注3:杂化泛函不能用 ICHARG=11 算能带,需用零权重 k 点方案
# 注4:线模式能带若 EIGENVAL 的 k 点数少于 KPOINTS,是被对称约化了,设 ISYM=0