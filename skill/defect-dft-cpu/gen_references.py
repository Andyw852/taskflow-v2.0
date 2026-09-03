#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_references.py —— 第0步：给凸包参考相（元素相 + 竞争二元相）生成 VASP 输入并提交。

在含 convex_hull_references/<相>/POSCAR 的目录下运行（jzzn 登录节点，能访问 POTCAR 库）。

用法：
    python3 gen_references.py [--potcar-dir /path/to/potpaw] [--submit]

统一参数（必须与缺陷计算一致，化学势才能相减）：
    PBE-D3(IVDW=12) + SOC(LSORBIT) + ENCUT=370 + PREC=Accurate
    金属(Pb/Sn)：ISMEAR=1 SIGMA=0.1；半金属/半导体：ISMEAR=0 SIGMA=0.05
    ISIF=3 全弛豫（POSCAR 是实验晶格常数，必须弛豫到各自平衡晶格）
    POTCAR 变体与 step.conf 一致：Pb_d / Sn_d / Sb / Bi_d / Te
"""
import sys, os, argparse, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

# name -> (ISMEAR, SIGMA, K 网格, 类型)
PHASES = {
    "Pb_fcc":    {"ismear": 1, "sigma": 0.10, "kmesh": (16, 16, 16), "kind": "金属"},
    "Sn_beta":   {"ismear": 1, "sigma": 0.10, "kmesh": (12, 12, 12), "kind": "金属"},
    "Sb_rhombo": {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半金属"},
    "SnSb":       {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半金属"},
    "Bi_rhombo": {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半金属"},
    "Te_trig":   {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半导体"},
    "PbTe_rs":   {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半导体"},
    "SnTe_rs":   {"ismear": 0, "sigma": 0.05, "kmesh": (12, 12, 12), "kind": "半导体"},
    "Sb2Te3":    {"ismear": 0, "sigma": 0.05, "kmesh": (8, 8, 2),  "kind": "半导体"},
    "Bi2Te3":    {"ismear": 0, "sigma": 0.05, "kmesh": (8, 8, 2),  "kind": "半导体"},
}

INCAR = """SYSTEM = ref {{name}}
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
ISMEAR = {{ismear}}
SIGMA  = {{sigma}}
ISPIN  = 1
ISYM   = 0
IBRION = 2
ISIF   = 3
NSW    = 60
EDIFFG = -0.02
LWAVE  = .FALSE.
LCHARG = .FALSE.
NCORE  = 8
KPAR   = 2
LSORBIT = .TRUE.
GGA_COMPAT = .FALSE.
LMAXMIX = 4
MAGMOM = {{magmom}}
"""

# 与 setting/jzzn/templates/submit_ncl_3d.tpl 一致（vasp_ncl，cpu192，24 核）
SUBMIT = """#!/bin/bash
#SBATCH --partition=cpu192
#SBATCH --job-name={{jobname}}
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=24
#SBATCH --output=queue.out
#SBATCH --error=queue.err
#SBATCH --qos=regular

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
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--potcar-dir", default="/public/home/wangchao/software/vasp_pseudopotentials")
    ap.add_argument("--submit", action="store_true", help="生成后立即 sbatch")
    ap.add_argument("--refdir", default="convex_hull_references")
    ap.add_argument("--only", default="", help="只处理这些相（逗号分隔），默认全部")
    ap.add_argument("--force", action="store_true", help="强制重写并重提交已完成（已收敛）的相")
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    refdir = Path(args.refdir)
    if not refdir.is_dir():
        raise SystemExit("[错误] 找不到 %s（请先把 POSCAR 放进 convex_hull_references/<相>/）" % refdir)

    n = 0
    for name, spec in PHASES.items():
        if only is not None and name not in only:
            continue
        d = refdir / name
        poscar = d / "POSCAR"
        if not poscar.exists():
            print("[跳过] %s 缺 POSCAR" % name)
            continue
        # 幂等保护：已收敛的相不重写不重提交（--force 例外）
        outcar = d / "OUTCAR"
        if outcar.exists() and not args.force:
            try:
                txt = outcar.read_text(errors="ignore")
                if "reached required accuracy" in txt:
                    print("[跳过] %s 已完成且收敛（--force 重算）" % name)
                    continue
            except OSError:
                pass
        st = D.parse_poscar(str(poscar))
        order = []
        for a in st["atoms"]:
            if a not in order:
                order.append(a)
        natoms = len(st["atoms"])
        incar = (INCAR.replace("{{name}}", name)
                     .replace("{{ismear}}", str(spec["ismear"]))
                     .replace("{{sigma}}", str(spec["sigma"]))
                     .replace("{{magmom}}", "%d*0" % (3 * natoms)))
        (d / "INCAR").write_text(incar, encoding="utf-8")
        D.write_kpoints(str(d / "KPOINTS"), spec["kmesh"])
        D.assemble_potcar(order, args.potcar_dir, out_path=str(d / "POTCAR"))
        (d / "submit.sh").write_text(SUBMIT.replace("{{jobname}}", "ref_" + name),
                                     encoding="utf-8")
        print("[OK] %-11s %2d 原子  %s  %s  k=%s" %
              (name, natoms, ",".join(order), spec["kind"], spec["kmesh"]))
        n += 1
        if args.submit:
            subprocess.run(["sbatch", "submit.sh"], cwd=str(d))
    print("共生成 %d 个参考相输入%s" % (n, "（已提交）" if args.submit else ""))

if __name__ == "__main__":
    main()
