#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_relax.py —— 用 MACE 势弛豫原胞（step1_mace_relax 的实际干活脚本）。

**在 conda 环境里跑**，cwd = step1_mace_relax/，读本目录 POSCAR，写 CONTCAR +
relax_summary.json + workflow_method.txt。

为什么必须在这里再弛豫一遍（哪怕你已经有 DFT 优化好的结构）：
力常数是在**势自身的能量极小点**上做泰勒展开。若在 DFT 极小点上取 MACE 的力，
残余力不为零，二阶力常数带上一次项污染，声学支在 Γ 附近直接掉成虚频——这是
MLIP 声子最常见的翻车原因，且看起来像"材料不稳定"。所以 RELAX=false 只在你
明确知道自己在干什么时用，且本脚本会把残余力当闸门卡住。

对称性：全程挂 ASE 的 FixSymmetry。不挂的话，数值噪声会把空间群降到 P1，
phono3py 的对称约化失效，位移数从几十个膨胀到上千个。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mace_model as mm

EV_A3_TO_GPA = 160.21766208


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--device", default="auto")
    p.add_argument("--dtype", default="float64")
    p.add_argument("--dim", default="3d")
    p.add_argument("--vac-axis", type=int, default=2)
    p.add_argument("--relax", default="true")
    p.add_argument("--relax-cell", default="true")
    p.add_argument("--fmax", type=float, default=1e-4)
    p.add_argument("--steps", type=int, default=2000)
    p.add_argument("--opt", default="FIRE")
    p.add_argument("--fix-symmetry", default="true")
    p.add_argument("--symprec", type=float, default=1e-4)
    p.add_argument("--cell-policy", default="primitive")   # primitive|none
    p.add_argument("--residual-tol", type=float, default=2e-3)
    p.add_argument("--stress-tol", type=float, default=0.0)  # GPa，<=0 不判
    return p.parse_args()


def as_bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on", ".true.")


def spg(atoms, symprec):
    try:
        import spglib
        cell = (atoms.get_cell()[:], atoms.get_scaled_positions(),
                atoms.get_atomic_numbers())
        return spglib.get_spacegroup(cell, symprec=symprec) or "unknown"
    except Exception as e:
        return "spglib unavailable (%s)" % e


def standardize_primitive(atoms, symprec):
    """spglib 取标准原胞。2D 不做——标准化会把真空轴转走，后面的 kz=1 约定就废了。"""
    import spglib
    from ase import Atoms
    cell = (atoms.get_cell()[:], atoms.get_scaled_positions(),
            atoms.get_atomic_numbers())
    res = spglib.standardize_cell(cell, to_primitive=True, no_idealize=False,
                                  symprec=symprec)
    if res is None:
        print("[WARN] spglib 标准化失败，沿用输入胞")
        return atoms
    lat, pos, nums = res
    return Atoms(numbers=nums, scaled_positions=pos, cell=lat, pbc=True)


def make_filter(atoms, dim, vac_axis, relax_cell):
    if not relax_cell:
        return atoms, "固定晶胞（只弛豫内坐标）"
    mask = None
    if dim == "2d":
        # Voigt 序 [xx, yy, zz, yz, xz, xy]：只放开面内两个轴长和面内剪切
        m = [1, 1, 1, 1, 1, 1]
        ax = vac_axis if vac_axis in (0, 1, 2) else 2
        m[ax] = 0                                   # 真空方向轴长锁死
        for i, pair in enumerate([(1, 2), (0, 2), (0, 1)]):
            if ax in pair:
                m[3 + i] = 0                        # 含真空方向的剪切锁死
        mask = m
    try:
        from ase.filters import FrechetCellFilter as CF
        name = "FrechetCellFilter"
    except ImportError:
        from ase.constraints import ExpCellFilter as CF
        name = "ExpCellFilter"
    return (CF(atoms, mask=mask) if mask else CF(atoms)), \
           "%s%s" % (name, ("，2D mask=%s" % mask) if mask else "")


def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    from ase.io import read as ase_read
    from ase.io import write as ase_write

    if not (cwd / "POSCAR").is_file():
        sys.exit("[ERROR] 本目录没有 POSCAR")
    atoms = ase_read("POSCAR", format="vasp")
    n0, v0 = len(atoms), atoms.get_volume()
    sg0 = spg(atoms, a.symprec)
    print("[..] 输入：%d 原子  V=%.3f Å³  空间群 %s" % (n0, v0, sg0))

    if a.cell_policy.lower() == "primitive" and a.dim != "2d":
        atoms = standardize_primitive(atoms, a.symprec)
        print("[OK] spglib 标准原胞：%d 原子  V=%.3f Å³" % (len(atoms), atoms.get_volume()))
    elif a.dim == "2d":
        print("[..] 2D：跳过 spglib 标准化（会把真空轴转离 c）")

    # fix-optmace：体积变化必须以【弛豫起点】为基准。原来用的是标准化
    # 之前的 v0，CELL_POLICY=primitive 且输入是惯用胞时会报出 -75% 的假变化。
    n_ref, v_ref = len(atoms), atoms.get_volume()

    calc, desc = mm.build_calculator(a.model, [cwd, cwd.parent, a.model_dir],
                                     a.device, a.dtype)
    print("[OK] MACE calculator：%s" % desc)
    atoms.calc = calc

    if as_bool(a.fix_symmetry):
        try:
            from ase.spacegroup.symmetrize import FixSymmetry
        except ImportError:
            try:
                from ase.constraints import FixSymmetry  # ASE >= 3.23 移到这里
            except ImportError:
                FixSymmetry = None
        if FixSymmetry is not None:
            atoms.set_constraint(FixSymmetry(atoms, symprec=a.symprec))
            print("[OK] FixSymmetry 已挂（symprec=%g）" % a.symprec)
        else:
            print("[WARN] 没装上 FixSymmetry，对称性可能被数值噪声降级")

    steps_done, converged = 0, True
    if as_bool(a.relax):
        target, fdesc = make_filter(atoms, a.dim, a.vac_axis, as_bool(a.relax_cell))
        print("[..] 优化器 %s，%s，fmax=%g eV/Å" % (a.opt, fdesc, a.fmax))
        import ase.optimize as aopt
        Opt = getattr(aopt, a.opt, None)
        if Opt is None:
            sys.exit("[ERROR] ase.optimize 里没有优化器 %r（用 FIRE / LBFGS / BFGS）" % a.opt)
        dyn = Opt(target, logfile="-", trajectory="relax.traj")
        dyn.run(fmax=a.fmax, steps=a.steps)
        steps_done = dyn.get_number_of_steps()
        # ASE 3.26 的 Optimizer.converged(gradient) 改成 1D gradient 必填，
        # 直接 dyn.converged() 会 TypeError；这里按最终原子力自判是否压到 fmax。
        # fix-optmace：优化目标是 target（relax_cell=true 时是 CellFilter，
        # 力+应力一起收敛），收敛就必须按同一个量判。原来只看
        # atoms.get_forces()（纯原子力），晶胞应力没收敛也会写 converged=true。
        converged = bool(np.max(np.linalg.norm(target.get_forces(), axis=1)) < a.fmax)
        if not converged:
            print("[FAIL] %d 步内没收敛到 fmax=%g。加大 STEPS，或先用松一点的 FMAX "
                  "看结构是不是在往合理方向走。" % (a.steps, a.fmax))
    else:
        print("[..] RELAX=false：不优化，只测残余力")

    atoms.set_constraint()          # 去掉 FixSymmetry 再取力，拿到真实残余力
    atoms.calc = calc
    forces = atoms.get_forces()
    fmax_now = float(np.max(np.linalg.norm(forces, axis=1)))
    energy = float(atoms.get_potential_energy())
    try:
        stress = atoms.get_stress(voigt=True) * EV_A3_TO_GPA
        smax = float(np.max(np.abs(stress)))
    except Exception:
        stress, smax = None, None
    sg1 = spg(atoms, a.symprec)

    if fmax_now > a.residual_tol:
        converged = False
        print("[FAIL] 残余力 %.2e eV/Å > RESIDUAL_TOL=%.2e —— 结构不在本势的极小点上，"
              "继续算声子会出假虚频。" % (fmax_now, a.residual_tol))
    else:
        print("[OK] 残余力 %.2e eV/Å ≤ %.2e" % (fmax_now, a.residual_tol))

    # fix-optmace：应力闸。原来 max_stress_GPa 只记录、从不判，
    # RELAX_CELL=true 时晶胞没弛豫到位也能写出 converged=true，形成能会偏。
    # 2D 只判面内分量——真空方向的轴长/剪切本来就被 mask 锁死，应力不为零是正常的。
    s_gate = smax
    if stress is not None and a.dim == "2d":
        _ax = a.vac_axis if a.vac_axis in (0, 1, 2) else 2
        _keep = [i for i in range(3) if i != _ax]
        _keep += [3 + i for i, _p in enumerate([(1, 2), (0, 2), (0, 1)])
                  if _ax not in _p]
        s_gate = float(np.max(np.abs([stress[i] for i in _keep])))
    if a.stress_tol > 0 and s_gate is not None:
        if s_gate > a.stress_tol:
            converged = False
            print("[FAIL] 残余应力 %.4f GPa > STRESS_TOL=%.4f —— 晶胞未"
                  "弛豫到位，总能和形成能会偏。" % (s_gate, a.stress_tol))
        else:
            print("[OK] 残余应力 %.4f GPa ≤ %.4f" % (s_gate, a.stress_tol))

    ase_write("CONTCAR", atoms, format="vasp", direct=True, sort=False)
    print("[OK] CONTCAR 已写出：%d 原子  V=%.3f Å³  空间群 %s"
          % (len(atoms), atoms.get_volume(), sg1))

    summary = {
        "converged": bool(converged),
        "relaxed": as_bool(a.relax),
        "n_atoms": len(atoms),
        "energy_eV": energy,
        "energy_per_atom_eV": energy / max(len(atoms), 1),
        "max_force_eV_per_A": fmax_now,
        "residual_tol": a.residual_tol,
        "max_stress_GPa": smax,
        "stress_voigt_GPa": (None if stress is None else [float(x) for x in stress]),
        "volume_A3": float(atoms.get_volume()),
        "volume_change_pct": 100.0 * (atoms.get_volume() - v_ref) / v_ref,
        "volume_ref_A3": float(v_ref), "n_atoms_ref": int(n_ref),
        "volume_input_A3": float(v0), "n_atoms_input": int(n0),
        "stress_tol_GPa": a.stress_tol,
        "max_stress_gated_GPa": s_gate,
        "spacegroup_in": sg0, "spacegroup_out": sg1,
        "opt_steps": int(steps_done), "model": desc, "dim": a.dim,
        "wall_time_s": round(time.time() - t0, 3),
    }
    Path("relax_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    if sg0.split("(")[0] != sg1.split("(")[0]:
        print("[WARN] 空间群从 %s 变成 %s。若不是有意为之，说明弛豫把结构带出了原相，"
              "后续位移数和声子谱都会跟着变。" % (sg0, sg1))
    el = time.time() - t0
    per_step = el / max(steps_done, 1)
    print("[DONE] converged=%s  用时 %.1f s（%d 步 ≈ %.2f s/步）"
          % (str(converged).lower(), el, steps_done, per_step))


if __name__ == "__main__":
    main()
