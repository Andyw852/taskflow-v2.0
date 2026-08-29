#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu step1：3x3x1 完美超胞 PBE-D3+SOC 弛豫（LVHAR=开，供静电势对齐）。"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

STEP = "step1_bulk"

def main():
    conf = D.load_stepconf()
    if not os.path.exists("POSCAR"):
        raise SystemExit("[错误] 材料目录缺 POSCAR（请放入已弛豫原胞 POSCAR_B）")
    prim = D.parse_poscar("POSCAR")
    n = tuple(int(x) for x in conf["SUPERCELL"].split())
    sc = D.supercell(prim, n)
    D.build_job(STEP, sc, conf, {
        "SYSTEM": "defect-dft-cpu step1_bulk PBE-D3+SOC supercell",
        "IBRION": "2", "ISIF": "2", "NSW": "60",
        "EDIFFG_LINE": "EDIFFG = -0.02",
        "LVHAR_LINE": "LVHAR = .TRUE.",
    }, "def_3x3x1_bulk")
    print("[OK] 生成 %s/（完美超胞 %d 原子）" % (STEP, len(sc["atoms"])))

if __name__ == "__main__":
    main()
