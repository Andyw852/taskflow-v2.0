#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mc_rattle.py —— hiphive MC-rattle 随机位移生成（与 kl-dft-cpu/lattice_kappa.py 同款）。

在 conda/venv 里跑（需要 hiphive + ase）。生成 n 个随机位移结构：每个原子的
位移按高斯分布（rattle_std），通过 probe 标定到目标 RMS；并设 d_min = dmin_scale
× 最近邻，保证原子不靠太近。比 phono3py/phonopy 的 --rd（固定模长）更物理。
"""
import math

import numpy as np


def nn_distance(atoms):
    from ase.neighborlist import neighbor_list
    for rc in (3.0, 4.0, 6.0, 8.0, 12.0):
        d = neighbor_list("d", atoms, rc)
        if len(d):
            return float(d.min())
    raise RuntimeError("无法确定最近邻距离")


def generate_calibrated_mc_rattle(atoms, n_struct, target_rms, dmin_scale,
                                  n_iter, seed, max_cal=5, tol=0.08, n_probe_cap=8):
    """返回 (structs, rattle_std, fin_rms)。structs 是位移后的 ase.Atoms 列表。"""
    from hiphive.structure_generation import generate_mc_rattled_structures
    ref = atoms.get_positions()
    nn = nn_distance(atoms)
    d_min = dmin_scale * nn
    print("[MC-rattle] 最近邻=%.3f Å -> d_min=%.3f Å (scale=%.2f)，目标 RMS=%.4f Å"
          % (nn, d_min, dmin_scale, target_rms))
    rattle_std = target_rms / math.sqrt(max(3 * n_iter, 1))
    n_probe = min(n_struct, n_probe_cap)
    ratio = float("nan")
    for it in range(max_cal):
        probe = generate_mc_rattled_structures(atoms, n_probe, rattle_std, d_min,
                                               seed=seed, n_iter=n_iter)
        mags = np.concatenate([np.linalg.norm(a.get_positions() - ref, axis=1)
                               for a in probe])
        rms = float(np.sqrt(np.mean(mags ** 2)))
        ratio = rms / target_rms
        print("  [MC-rattle 标定 %d] rattle_std=%.5f -> RMS=%.4f (比值 %.2f)"
              % (it + 1, rattle_std, rms, ratio))
        if abs(ratio - 1.0) <= tol:
            break
        rattle_std /= ratio
    else:
        print("[WARN] 标定 %d 次后比值仍 %.2f，按当前 rattle_std 继续"
              % (max_cal, ratio))
    final = generate_mc_rattled_structures(atoms, n_struct, rattle_std, d_min,
                                           seed=seed + 1, n_iter=n_iter)
    mags = np.concatenate([np.linalg.norm(a.get_positions() - ref, axis=1)
                           for a in final])
    fin_rms, fin_max = float(np.sqrt((mags ** 2).mean())), float(mags.max())
    print("[MC-rattle] 生成 %d 帧: RMS=%.4f Å, max|Δr|=%.4f Å" % (len(final), fin_rms, fin_max))
    return final, rattle_std, fin_rms
