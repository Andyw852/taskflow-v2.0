#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_static.py —— MACE 静态单点（step2_mace_static 的实际干活脚本）。

在 conda/venv 环境里跑，cwd = step2_mace_static/，读本目录 POSCAR，写
static_summary.json（E_tot / E_per_atom / 最大残余力）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mace_model as mm


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--device", default="cpu")
    p.add_argument("--dtype", default="float64")
    return p.parse_args()


def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    from ase.io import read as ase_read

    if not (cwd / "POSCAR").is_file():
        sys.exit("[ERROR] 本目录没有 POSCAR")
    atoms = ase_read("POSCAR", format="vasp")

    calc, desc = mm.build_calculator(a.model, [cwd, cwd.parent, a.model_dir],
                                     a.device, a.dtype)
    atoms.calc = calc
    e = float(atoms.get_potential_energy())
    forces = atoms.get_forces()
    fmax = float(np.max(np.linalg.norm(forces, axis=1)))

    summary = {
        "STATIC_DONE": True,
        "n_atoms": len(atoms),
        "energy_eV": e,
        "energy_per_atom_eV": e / max(len(atoms), 1),
        "max_force_eV_per_A": fmax,
        "model": desc,
        "wall_time_s": round(time.time() - t0, 3),
    }
    Path("static_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] E_tot=%.6f eV  E_per_atom=%.6f eV  max_force=%.3e eV/A  用时 %.1f s"
          % (e, summary["energy_per_atom_eV"], fmax, summary["wall_time_s"]))


if __name__ == "__main__":
    main()
