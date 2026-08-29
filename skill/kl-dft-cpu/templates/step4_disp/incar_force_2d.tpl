# phono3py 位移超胞单点取力（2D）。声子硬约束：ISYM=0（位移破缺对称，勿对称化力）、
# LREAL=.FALSE.（倒空间投影，力无噪声）、EDIFF=1E-8、PREC=Accurate。
SYSTEM  = {{SYSTEM}}
ISTART  = 0
ICHARG  = 2
GGA     = {{GGA}}
{{VDW_LINE}}

PREC    = Normal
ENCUT   = {{ENCUT}}
EDIFF   = 1E-7
LREAL   = Auto
LASPH   = .TRUE.
ALGO    = Normal
NELM    = 200
NELMIN  = 6
AMIN    = 0.01
ISMEAR  = 0
SIGMA   = 0.05

IBRION  = -1
NSW     = 0
ISYM    = 0

LWAVE   = .FALSE.
LCHARG  = .FALSE.
LORBIT  = 0
NCORE   = 4
KPAR    = 2
