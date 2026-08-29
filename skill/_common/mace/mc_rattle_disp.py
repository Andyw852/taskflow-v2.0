#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mc_rattle_disp.py —— hiphive MC-rattle 随机位移 → phono3py_disp.yaml（type-2）。

klmace S2 的 random 位移生成：替代 phono3py -d --rd（固定模长）。生成 N 个
MC-rattle 位移（目标 RMS + d_min 保护）写进 phono3py 的 type-2 dataset；
给了 FC2_SUPERCELL 时，fc2 专用超胞也走 MC-rattle（phonon_dataset）。
"""
import os
import sys

import numpy as np
from phonopy.interface.calculator import read_crystal_structure
from phono3py import Phono3py
from phonopy.interface.vasp import write_vasp
from ase import Atoms

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mc_rattle  # noqa: E402


def _rattle(supercell, n, rms, dmin, niter, seed):
    atoms = Atoms(numbers=supercell.numbers, positions=supercell.positions,
                  cell=supercell.cell, pbc=True)
    ref = atoms.get_positions()
    structs, rattle_std, rms_out = mc_rattle.generate_calibrated_mc_rattle(
        atoms, n, rms, dmin, niter, seed)
    disps = np.array([s.get_positions() - ref for s in structs], dtype=float)
    return disps, rattle_std, rms_out


def main():
    poscar, reps_s, n_s, rms_s, dmin_s, niter_s, seed_s = sys.argv[1:8]
    reps2_s, n2_s = (sys.argv[8], sys.argv[9]) if len(sys.argv) >= 10 else ("", "0")
    reps = [int(x) for x in reps_s.split()]
    n = int(n_s)
    rms = float(rms_s)
    dmin = float(dmin_s)
    niter = int(niter_s)
    seed = int(seed_s)

    cell, _ = read_crystal_structure(poscar, interface_mode="vasp")
    reps2 = [int(x) for x in reps2_s.split()] if reps2_s.strip() else None
    ph3 = Phono3py(cell, supercell_matrix=np.diag(np.array(reps, dtype=int)),
                   primitive_matrix=np.eye(3),
                   phonon_supercell_matrix=(np.diag(np.array(reps2, dtype=int))
                                            if reps2 else None))
    sc = ph3.supercell
    write_vasp("SPOSCAR", sc, direct=True)

    disps, rattle_std, rms_out = _rattle(sc, n, rms, dmin, niter, seed)
    if disps.shape != (n, len(sc.numbers), 3):
        sys.exit("[ERROR] fc3 位移形状异常 %s" % (disps.shape,))
    ph3.dataset = {"displacements": disps}

    n2 = 0
    if reps2:
        n2 = int(n2_s)
        sc2 = ph3.phonon_supercell
        disps2, _, _ = _rattle(sc2, n2, rms, dmin, niter, seed + 1000)
        ph3.phonon_dataset = {"displacements": disps2}
        write_vasp("SPOSCAR_FC2", sc2, direct=True)

    ph3.save("phono3py_disp.yaml")
    for i, s in enumerate(ph3.supercells_with_displacements, 1):
        if s is not None:
            write_vasp("POSCAR-%05d" % i, s, direct=True)
    print("MC_RATTLE_DONE n=%d n2=%d rms=%.4f rattle_std=%.5f"
          % (len(disps), n2, rms_out, rattle_std))


if __name__ == "__main__":
    main()
