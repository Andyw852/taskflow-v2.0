#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phonon_fit_driver.py —— phonopy 收力 + fc2 拟合 + 声子谱（phonon-dft-cpu S3）。

计算节点作业里跑，cwd = step3_phonon/：读 POSCAR + kl_params（SUPERCELL/FD_DISTANCE/MESH）
→ 收 ../step2_disp/disp-*/vasprun.xml 的力 → produce_force_constants →
q-mesh 最小频率（虚频闸）+ band-dft-cpu.yaml → phonon_summary.json。
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


def read_vasprun_force(path):
    """从 vasprun.xml 读最后一个结构块的力（N 原子 × 3）。"""
    import xml.etree.ElementTree as ET
    tree = ET.parse(str(path))
    forces = None
    for calc in tree.getroot().iter("calculation"):
        for va in calc.iter("varray"):
            if va.get("name") == "forces":
                rows = [[float(x) for x in (v.text or "").split()]
                        for v in va.findall("v")]
                if rows:
                    forces = rows
    if forces is None:
        raise RuntimeError("vasprun.xml 里没有 forces")
    return np.array(forces, dtype=float)


def main():
    cwd = Path.cwd()
    from phonopy import Phonopy
    from phonopy.interface.vasp import read_vasp

    if not (cwd / "POSCAR").is_file():
        sys.exit("[ERROR] 缺 POSCAR")
    params = read_params(cwd / "kl_params.txt")
    dim = [int(x) for x in (params.get("SUPERCELL") or "1 1 1").split()]
    fd = float(params.get("FD_DISTANCE") or 0.01)
    mesh = [int(x) for x in (params.get("MESH") or "20 20 20").split()]
    imag_thr = float(params.get("IMAG_THR") or 0.10)

    uc = read_vasp("POSCAR")
    ph = Phonopy(uc, supercell_matrix=np.diag(np.array(dim, dtype=int)),
                 primitive_matrix="auto")
    ph.generate_displacements(distance=fd)
    n = len(ph.supercells_with_displacements)
    print("[..] 超胞=%s 位移=%d 帧 FD_DISTANCE=%s" % (" ".join(str(x) for x in dim), n, fd))

    forces = []
    for i in range(1, n + 1):
        vasprun = cwd.parent / "step2_disp" / ("disp-%05d" % i) / "vasprun.xml"
        if not vasprun.is_file():
            sys.exit("[ERROR] 缺 %s" % vasprun)
        forces.append(read_vasprun_force(vasprun))
    ph.forces = np.array(forces, dtype=float)
    print("[OK] 收力 %d 帧" % len(forces))

    ph.produce_force_constants()
    print("[OK] fc2 拟合完成")

    ph.run_mesh(mesh=mesh, with_eigenvectors=False, is_mesh_symmetry=True)
    mf = float(np.min(ph.get_mesh_dict()["frequencies"]))
    try:
        ph.auto_band_structure(plot=False, write_yaml=True, filename="band-dft-cpu.yaml")
    except Exception as e:
        print("[WARN] band-dft-cpu.yaml 出图跳过：%s" % e)

    stable = mf >= -imag_thr
    summary = {
        "PHONON_DONE": True,
        "tool_ok": True,
        "stable": bool(stable),
        "min_frequency_THz": mf,
        "n_disp": int(n),
        "supercell": " ".join(str(x) for x in dim),
        "note": "最小声子频率 %.4f THz（阈值 -%.2f）：%s"
                % (mf, imag_thr, "无明显虚频" if stable else "存在虚频，动力学不稳定"),
    }
    (cwd / "phonon_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    print("[DONE] stable=%s min_freq=%.4f THz" % (str(stable).lower(), mf))


if __name__ == "__main__":
    main()
