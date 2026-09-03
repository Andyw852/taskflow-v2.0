#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phonon_fit_driver.py —— 2 阶力常数拟合 + 声子谱（phonon-mace-cpu S3）。

计算节点作业里跑，cwd = step3_phonon/：读 POSCAR + disps.npy + forces.npy →
symfc 拟合 fc2 → q-mesh 最小频率（虚频闸）+ band-dft-cpu.yaml → phonon_summary.json。
"""
import json
import sys
from pathlib import Path

import numpy as np


def read_params(path):
    d = {}
    p = Path(path)
    if p.is_file():
        for ln in p.read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                d[k.strip().upper()] = v.strip()
    return d


def main():
    cwd = Path.cwd()
    from phonopy import Phonopy
    from phonopy.interface.vasp import read_vasp

    for f in ("POSCAR", "disps.npy", "forces.npy"):
        if not (cwd / f).is_file():
            sys.exit("[ERROR] 缺 %s（step2 取力没跑完？）" % f)

    uc = read_vasp("POSCAR")
    params = read_params(cwd / "klmace_params.txt")
    dim = [int(x) for x in (params.get("SUPERCELL") or "1 1 1").split()]
    ph = Phonopy(uc, supercell_matrix=np.diag(np.array(dim, dtype=int)))

    disps = np.load("disps.npy")
    forces = np.load("forces.npy")
    ph.displacements = disps
    ph.forces = forces

    # symfc dense 求解器的正规方程 X^T X 内存 ~ O(basis^2)，大 supercell 会爆内存
    # （qHPC60 976 原子：无 cutoff 需 ~585 GiB）。cutoff 截断 fc2 非零范围，与
    # MACE 势 r_max=6.0 A 一致（6 A 外力常数本就为 0），不损失物理信息。
    fc_cutoff = params.get("FC_CUTOFF") or "6.0"
    print("[..] %d 帧 × %d 原子，symfc 拟合 fc2 (cutoff=%s A)"
          % (len(disps), len(uc.numbers) * int(np.prod(dim)), fc_cutoff))

    ph.produce_force_constants(
        fc_calculator="symfc",
        fc_calculator_options="cutoff = %s" % fc_cutoff,
    )
    print("[OK] fc2 拟合完成")

    ph.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
    mf = float(np.min(ph.get_mesh_dict()["frequencies"]))
    try:
        ph.auto_band_structure(plot=False, write_yaml=True, filename="band-dft-cpu.yaml")
    except Exception as e:
        print("[WARN] band-dft-cpu.yaml 出图跳过：%s" % e)

    stable = mf >= -0.10
    summary = {
        "PHONON_DONE": True,
        "stable": bool(stable),
        "min_frequency_THz": mf,
        "n_disp": int(len(disps)),
        "supercell": " ".join(str(x) for x in dim),
        "note": "最小声子频率 %.4f THz（阈值 -0.10）：%s"
                % (mf, "无明显虚频" if stable else "存在虚频，动力学不稳定"),
    }
    (cwd / "phonon_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] stable=%s min_freq=%.4f THz" % (str(stable).lower(), mf))


if __name__ == "__main__":
    main()
