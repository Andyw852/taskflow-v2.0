#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_forces_phonon.py —— MACE 随机位移取力（仅 2 阶，phonon-mace-cpu S2）。

在 conda/venv 里跑（计算节点作业），cwd = step2_disp_force/：
读 POSCAR（原胞）→ 扩超胞 → hiphive MC-rattle 生成 N 个随机位移（目标 RMS 0.01 Å）
→ 逐帧 MACE 取力（扣未位移超胞残余力）→ 落 disps.npy / forces.npy +
forces_summary.json。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mace_model as mm
import mc_rattle


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--device", default="cpu")
    p.add_argument("--dtype", default="float64")
    p.add_argument("--dim", required=True)             # "4 4 4"
    p.add_argument("--ndisp", type=int, required=True)  # ALM 反推出的帧数
    p.add_argument("--amplitude", type=float, default=0.01)   # 目标 RMS(Å)
    p.add_argument("--dmin-scale", type=float, default=0.85)  # d_min = 最近邻×scale
    p.add_argument("--n-iter", type=int, default=10)          # MC 迭代数
    p.add_argument("--seed", type=int, default=2025)
    return p.parse_args()


def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    from phonopy import Phonopy
    from phonopy.interface.vasp import read_vasp, write_vasp

    if not (cwd / "POSCAR").is_file():
        sys.exit("[ERROR] 本目录没有 POSCAR")
    uc = read_vasp("POSCAR")
    dim = [int(x) for x in a.dim.split()]
    ph = Phonopy(uc, supercell_matrix=np.diag(np.array(dim, dtype=int)))
    sc = ph.supercell
    write_vasp("SPOSCAR", sc, direct=True)

    base = mm.phonopy_atoms_to_ase(sc)
    base_pos = base.get_positions()
    structs, rattle_std, rms = mc_rattle.generate_calibrated_mc_rattle(
        base, a.ndisp, a.amplitude, a.dmin_scale, a.n_iter, a.seed)
    disps = np.array([s.get_positions() - base_pos for s in structs], dtype=float)
    print("[..] 超胞 %d 原子 × %d 个 MC-rattle 位移，RMS=%.4f Å"
          % (len(sc.numbers), len(disps), rms))

    calc, desc = mm.build_calculator(a.model, [cwd, cwd.parent, a.model_dir],
                                     a.device, a.dtype)
    base.calc = calc
    f0 = np.array(base.get_forces(), dtype=float)
    f0max = float(np.max(np.linalg.norm(f0, axis=1)))
    print("[..] 未位移超胞残余力 max=%.3e eV/Å" % f0max)

    forces = []
    t_loop = time.time()
    for i, d in enumerate(disps):
        base.set_positions(base_pos + d)
        f = np.array(base.get_forces(), dtype=float) - f0
        forces.append(f)
        if (i + 1) % 20 == 0 or i + 1 == len(disps):
            el = time.time() - t_loop
            rate = (i + 1) / max(el, 1e-9)
            eta = (len(disps) - i - 1) / max(rate, 1e-9)
            print("[..] %d/%d  %.2f 帧/s  ETA %.1f min"
                  % (i + 1, len(disps), rate, eta / 60.0), flush=True)
    forces = np.array(forces, dtype=float)

    np.save("disps.npy", disps)
    np.save("forces.npy", forces)
    try:
        ph.displacements = disps
        ph.save("phonopy_disp.yaml")
    except Exception as e:
        print("[WARN] 写 phonopy_disp.yaml 失败：%s" % e)

    el = time.time() - t0
    Path("forces_summary.json").write_text(json.dumps({
        "FORCES_DONE": True,
        "n_disp": int(len(disps)),
        "n_atoms_supercell": int(len(sc.numbers)),
        "residual_force_max_eV_per_A": f0max,
        "displacement_rms_A": rms,
        "rattle_std_A": rattle_std,
        "model": desc,
        "wall_time_s": round(el, 3),
        "sec_per_frame": round(el / max(len(disps), 1), 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] 取力完成：%d 帧，用时 %.1f min（%.2f s/帧）"
          % (len(disps), el / 60.0, el / max(len(disps), 1)))


if __name__ == "__main__":
    main()
