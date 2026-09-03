#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_isif2_static.py —— p8 弛豫晶格 -> 3x3x1 超胞 -> ISIF=2 静态(KMESH 2 2 2)。

量 δ = E_super_new/9 - E_prim(p8)：把超胞晶格换成 p8 的弛豫晶格后做静态单点，
隔离超胞 vs 原胞的 k 收敛/尺寸残差（排除 ISIF=2 冻错晶格的混淆）。

用法：python3 run_isif2_static.py [原胞CONTCAR] [输出目录]
"""
import sys, os
sys.path.insert(0, '/public/home/wangchao/defect_work/Sn2Sb2Te5/defect-dft-cpu')
import defects_common as D

def main():
    prim_contcar = sys.argv[1] if len(sys.argv) > 1 else 'convex_hull_references/Sn2Sb2Te5_p8/CONTCAR'
    outdir = sys.argv[2] if len(sys.argv) > 2 else 'convex_hull_references/Sn2Sb2Te5_super_static'
    if not os.path.exists(prim_contcar):
        raise SystemExit('[错误] 找不到 %s（先等 p8 收敛）' % prim_contcar)
    st = D.parse_poscar(prim_contcar)
    sc = D.supercell(st, (3, 3, 1))
    os.makedirs(outdir, exist_ok=True)
    D.write_poscar(os.path.join(outdir, 'POSCAR'), sc, comment='Sn2Sb2Te5 3x3x1 from p8 relaxed lattice')
    # INCAR: ISIF=2 静态（不弛豫原子，只固定晶格算总能），KMESH 2 2 2
    incar = '''SYSTEM = Sn2Sb2Te5 super static (ISIF=2 from p8 lattice)
ISTART = 0
ICHARG = 2
GGA    = PE
IVDW   = 12
PREC   = Accurate
ENCUT  = 370
LREAL  = .FALSE.
LASPH  = .TRUE.
ALGO   = All
AMIX   = 0.1
BMIX   = 0.0001
EDIFF  = 1E-6
NELM   = 200
NELMIN = 6
ISMEAR = 0
SIGMA  = 0.05
ISPIN  = 1
ISYM   = 0
IBRION = -1
ISIF   = 2
NSW    = 0
LWAVE  = .FALSE.
LCHARG = .FALSE.
NCORE  = 8
KPAR   = 2
LSORBIT = .TRUE.
GGA_COMPAT = .FALSE.
LMAXMIX = 4
MAGMOM = 243*0
'''
    open(os.path.join(outdir, 'INCAR'), 'w').write(incar)
    open(os.path.join(outdir, 'KPOINTS'), 'w').write('Auto mesh (supercell)\n0\nGamma\n2 2 2\n0 0 0\n')
    D.assemble_potcar(['Sb', 'Te', 'Sn'], '/public/home/wangchao/software/vasp_pseudopotentials',
                       out_path=os.path.join(outdir, 'POTCAR'))
    sub = '''#!/bin/bash
#SBATCH --partition=cpu192
#SBATCH --job-name=Sn2Sb2Te5_static
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=24
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=premium

cd $SLURM_SUBMIT_DIR

conda deactivate 2>/dev/null
conda deactivate 2>/dev/null
module purge
unset LD_LIBRARY_PATH

set --
source /public/software/intel/2022.3/setvars.sh --force > /dev/null 2>&1
module load vasp/6.4.3-oneapi2022.3
export OMP_NUM_THREADS=1

mpirun -np $SLURM_NTASKS vasp_ncl
'''
    open(os.path.join(outdir, 'submit.sh'), 'w').write(sub)
    print('[OK] ISIF=2 静态超胞就绪:', outdir)
    print('     81 原子, KMESH 2 2 2, 晶格来自 p8 CONTCAR')
    print('     跑完用: E_super_new/9 - E_prim(p8) = δ')

if __name__ == '__main__':
    main()