SYSTEM   = {{SYSTEM}}

# ---- 基本精度 ----
PREC     = Accurate
ENCUT    = {{ENCUT}}
EDIFF    = 1E-6
GGA      = {{GGA}}
{{VDW_LINE}}
LASPH    = .TRUE.          # 非球形梯度修正；含 d/f 客体原子时明显影响能量

# ---- 展宽：孤立分子是分立能级，必须用高斯展宽 ----
ISMEAR   = 0
SIGMA    = 0.05

# ---- 结构弛豫：固定晶胞 ----
# 真空对能量没有贡献，ISIF=3 会把盒子一路压塌到贴着分子，
# 于是每个体系的盒子都不一样，总能再也没法横向比。0D 只能 ISIF=2。
ISIF     = 2
IBRION   = 2
POTIM    = 0.2
NSW      = 300
EDIFFG   = -0.01           # 分子不必死磕到 -0.001（那是给力常数留的）

# ---- 分子专用 ----
ISYM     = 0               # 偏心的客体原子别被对称化拉回高对称位
LREAL    = .FALSE.         # 实空间投影的力噪声底在 1e-3，会卡住紧判据
KPAR     = 1               # 只有一个不可约 k 点，KPAR>1 无意义
NCORE    = 8               # 按节点核数调

# ---- 偶极修正 ----
# M@C60 实际是 M(+)@C60(-)，有净偶极；周期镜像间的偶极-偶极作用
# 在 20 Å 盒子里是十几 meV 量级，恰好是排包合能顺序的分辨率。
LDIPOL   = .TRUE.
IDIPOL   = 4
DIPOL    = {{DIPOL}}       # gen 脚本算的结构几何中心（分数坐标）

# ---- 自旋（gen 脚本按 POTCAR 价电子奇偶 + 元素表算出）----
ISPIN    = {{ISPIN}}
MAGMOM   = {{MAGMOM}}

# ---- 收敛控制 ----
NELM     = 200
NELMIN   = 6
ALGO     = Normal
LWAVE    = .FALSE.
LCHARG   = .FALSE.
