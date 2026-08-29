#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""计算 38 种金属单质每原子 MACE 总能（形成能参考化学势 μ）。
CPU 版，跑 jzzn mace_cpu venv。
输出：results.json + 逐元素打印。
"""
import json
import os
from ase.build import bulk
from ase.io import write
from ase.optimize import BFGS
from ase.filters import FrechetCellFilter

MODEL = os.environ.get("MACE_MODEL_PATH", "/home/wangchao/software/taskflow/skill/kl-mace-cpu/templates/mace/MACE-matpes-pbe-omat-ft.model")

# 金属 -> (晶型, 晶格常数 A)；按元素常用晶型填写
STRUCT = {
    "Ag": ("fcc", 4.085), "Al": ("fcc", 4.05), "Au": ("fcc", 4.08),
    "Ba": ("bcc", 5.02), "Be": ("hcp", 2.29), "Ca": ("fcc", 5.59),
    "Cd": ("hcp", 2.98), "Co": ("hcp", 2.51), "Cr": ("bcc", 2.88),
    "Cs": ("bcc", 6.14), "Cu": ("fcc", 3.61), "Fe": ("bcc", 2.87),
    "Hf": ("hcp", 3.19), "Ir": ("fcc", 3.84), "K": ("bcc", 5.23),
    "Li": ("bcc", 3.51), "Mg": ("hcp", 3.21), "Mn": ("aMn", 8.9125),
    "Mo": ("bcc", 3.15), "Na": ("bcc", 4.29), "Nb": ("bcc", 3.3),
    "Ni": ("fcc", 3.52), "Os": ("hcp", 2.74), "Pd": ("fcc", 3.89),
    "Pt": ("fcc", 3.92), "Rb": ("bcc", 5.72), "Re": ("hcp", 2.76),
    "Rh": ("fcc", 3.80), "Ru": ("hcp", 2.71), "Sc": ("hcp", 3.31),
    "Sr": ("fcc", 6.08), "Ta": ("bcc", 3.31), "Ti": ("hcp", 2.95),
    "V": ("bcc", 3.03), "W": ("bcc", 3.17), "Y": ("hcp", 3.65),
    "Zn": ("hcp", 2.67), "Zr": ("hcp", 3.23),
}


def make_atoms(el, ctype, a):
    from ase.spacegroup import crystal
    if ctype == "aMn":  # α-Mn, space group 217 (I-43m), 58 atoms
        basis = [(0, 0, 0), (0.317, 0.317, 0.317),
                 (0.356, 0.356, 0.036), (0.089, 0.089, 0.282)]
        return crystal(["Mn"] * 4, basis, cellpar=[a, a, a, 90, 90, 90],
                       spacegroup=217, primitive_cell=False)
    if ctype == "fcc":
        return bulk(el, "fcc", a=a, cubic=True)
    if ctype == "bcc":
        return bulk(el, "bcc", a=a, cubic=True)
    if ctype == "hcp":
        return bulk(el, "hcp", a=a, c=1.62 * a)


def main():
    from mace.calculators import MACECalculator
    calc = MACECalculator(model_paths=MODEL, device="cpu", default_dtype="float64")
    results = {}
    for el in sorted(STRUCT):
        ctype, a = STRUCT[el]
        atoms = make_atoms(el, ctype, a)
        atoms.set_calculator(calc)
        try:
            atoms = atoms.copy()
            atoms.set_calculator(calc)
            opt = BFGS(FrechetCellFilter(atoms), logfile=None)
            converged = opt.run(fmax=1e-3, steps=2000)
            e_per_atom = atoms.get_potential_energy() / len(atoms)
            results[el] = {"ctype": ctype, "a": a, "n": len(atoms),
                           "E_per_atom": e_per_atom, "converged": bool(converged)}
            print("%s: %s a=%.4f n=%d E/atom=%.5f converged=%s"
                  % (el, ctype, a, len(atoms), e_per_atom, converged), flush=True)
        except Exception as e:
            results[el] = {"ctype": ctype, "a": a, "error": str(e)}
            print("%s: ERROR %s" % (el, e), flush=True)
    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("DONE")


if __name__ == "__main__":
    main()
