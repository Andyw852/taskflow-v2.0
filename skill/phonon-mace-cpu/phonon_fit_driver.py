#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phonon_fit_driver.py —— 2 阶力常数拟合 + 声子谱（phonon-mace-cpu S3）。

计算节点作业里跑，cwd = step3_phonon/：读 POSCAR + disps.npy + forces.npy →
symfc 拟合 fc2 → q-mesh 最小频率（虚频闸）+ band-dft-cpu.yaml → phonon_summary.json。
"""
import json
import re
import sys
from pathlib import Path

import numpy as np


def _read_force_constants(path):
    try:
        from phonopy.file_IO import read_force_constants_hdf5
        return read_force_constants_hdf5(path)
    except ImportError:
        from phonopy.file_IO import parse_FORCE_CONSTANTS
        return parse_FORCE_CONSTANTS(path)


def _export_force_constants(ph, cwd):
    """Best-effort standard fc2 exports; export failures do not abort phonons."""
    try:
        from phonopy.file_IO import write_force_constants_to_hdf5
        write_force_constants_to_hdf5(ph.force_constants, filename=str(cwd / "fc2.hdf5"))
        print("[OK] 已写出 fc2.hdf5")
    except Exception as e:
        print("[WARN] fc2.hdf5 导出失败：%s" % e)
    try:
        from phonopy.file_IO import write_FORCE_CONSTANTS
        write_FORCE_CONSTANTS(ph.force_constants, filename=str(cwd / "FORCE_CONSTANTS"))
        print("[OK] 已写出 FORCE_CONSTANTS")
    except Exception as e:
        print("[WARN] FORCE_CONSTANTS 导出失败：%s" % e)


def _nfree_fc2_alm(ph, cutoff_a):
    """估算给定 cutoff 下不可约 2 阶力常数元数（与 gen_step2 的 ALM 反推同源）。

    位移帧数 n_disp 是否足以确定 symfc 拟合：方程数 = n_disp × 3N_sc（每个原子
    3 个力分量），未知数 = 不可约 2 阶力常数元数 nfree_fc2。方程数 < 未知数即拟合
    欠定，此时虚频不可信。缺 alm/结构异常时返回 None（不阻塞拟合，只把
    fit_underdetermined 记为 null）。
    """
    try:
        from ase import Atoms
        from alm import ALM
    except Exception:
        return None
    try:
        sc = ph.supercell
        atoms = Atoms(numbers=sc.numbers, positions=sc.positions, cell=sc.cell, pbc=True)
        nkd = len(set(atoms.numbers))
        cut = np.full((1, nkd, nkd), float(cutoff_a), dtype=float)
        with ALM(np.array(atoms.cell), atoms.get_scaled_positions(),
                 atoms.get_atomic_numbers(), verbosity=0) as a:
            a.define(1, cutoff_radii=cut)
            a.suggest()
            return int(a._get_number_of_irred_fc_elements(1))
    except Exception as e:
        print("[WARN] ALM 数 2 阶力常数元数失败：%s" % e)
        return None


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
    _dim = np.array([int(x) for x in (params.get("SUPERCELL") or "1 1 1").split()],
                    dtype=int)
    # 3 个数=对角扩胞；9 个数=3×3 矩阵（行主序，与 phonopy/phono3py --dim 同义）
    scm = _dim.reshape(3, 3) if _dim.size == 9 else np.diag(_dim)
    ph = Phonopy(uc, supercell_matrix=scm, primitive_matrix="P")

    disps = np.ascontiguousarray(np.load("disps.npy"), dtype="double")
    forces = np.ascontiguousarray(np.load("forces.npy"), dtype="double")
    prefit = cwd / "fc2.hdf5"
    if not prefit.is_file():
        ph.dataset = {"displacements": disps, "forces": forces}

    # symfc dense 求解器的正规方程 X^T X 内存 ~ O(basis^2)，大 supercell 会爆内存
    # （qHPC60 976 原子：无 cutoff 需 ~585 GiB）。cutoff 截断 fc2 非零范围，与
    # FC_CUTOFF 是计算/建模截断；MACE receptive fields 与多体混合导数意味着
    # r_max 不能证明 fc2 在其外严格为零，必须通过 cutoff 收敛性检查验证。
    fc_cutoff = params.get("FC_CUTOFF") or "6.0"
    print("[..] %d 帧 × %d 原子，symfc 拟合 fc2 (cutoff=%s A)"
          % (len(disps), len(uc.numbers) * int(np.prod(dim)), fc_cutoff))

    if prefit.is_file():
        print("[..] 使用 pheasy-gpu 预拟合 fc2.hdf5")
        ph.force_constants = _read_force_constants(prefit)
    else:
        ph.produce_force_constants(
            fc_calculator="symfc",
            fc_calculator_options="cutoff = %s" % fc_cutoff,
        )
    print("[OK] fc2 拟合完成")
    _export_force_constants(ph, cwd)

    ph.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
    mesh_freqs = np.asarray(ph.get_mesh_dict()["frequencies"], dtype=float)
    if mesh_freqs.size == 0 or not np.all(np.isfinite(mesh_freqs)):
        sys.exit("[ERROR] mesh frequencies 为空或含非有限值")
    mf = float(np.min(mesh_freqs))
    mx = float(np.max(mesh_freqs))
    try:
        ph.auto_band_structure(plot=False, write_yaml=True, filename="band-dft-cpu.yaml")
    except Exception as e:
        sys.exit("[ERROR] band-dft-cpu.yaml 生成失败：%s" % e)

    band_text = (cwd / "band-dft-cpu.yaml").read_text(encoding="utf-8", errors="replace")
    band_values = [float(x) for x in re.findall(r"^\s*frequency:\s*([^\s#]+)", band_text, re.MULTILINE)]
    band_freqs = np.asarray(band_values, dtype=float)
    if band_freqs.size == 0 or not np.all(np.isfinite(band_freqs)):
        sys.exit("[ERROR] band frequencies empty or nonfinite")
    bmf = float(np.min(band_freqs))
    bmx = float(np.max(band_freqs))
    screen_min = min(mf, bmf)
    stable_mesh = mf >= -0.10
    stable = screen_min >= -0.10

    # Chemistry-specific quality flag only; not proof of a failed fit.
    bound = None
    raw_bound = params.get("MAX_FREQ_MIN_THZ")
    if raw_bound not in (None, ""):
        try:
            bound = float(raw_bound)
        except (TypeError, ValueError):
            sys.exit("[ERROR] MAX_FREQ_MIN_THZ must be finite and positive")
        if not np.isfinite(bound) or bound <= 0:
            sys.exit("[ERROR] MAX_FREQ_MIN_THZ must be finite and positive")
    fit_suspect = bound is not None and max(mx, bmx) < bound
    note = "联合采样最低频率 %.4f THz（容差 -0.10）：%s" % (
        screen_min, "通过数值筛查" if stable else "存在超容差虚频")
    if fit_suspect:
        note += "；最高频率低于项目复核阈值，仅标可疑，不证明拟合失败"
        print("[WARN] " + note)

    try:
        fc_cutoff_A = float(fc_cutoff)
    except (TypeError, ValueError):
        fc_cutoff_A = None

    # 位移帧数是否足以确定 fc2：方程数 = n_disp × 3N_sc，未知数 = nfree_fc2(ALM)
    n_sc = int(len(uc.numbers) * np.prod(dim))
    n_disp = int(len(disps))
    dof = 3 * n_sc
    nfree_fc2 = _nfree_fc2_alm(ph, fc_cutoff_A) if fc_cutoff_A else None
    if nfree_fc2 is None or nfree_fc2 <= 0:
        frame_margin = None
        fit_underdetermined = None
    else:
        frame_margin = round((n_disp * dof) / float(nfree_fc2), 3)
        fit_underdetermined = bool(n_disp * dof < nfree_fc2)
    if fit_underdetermined:
        note += "；位移帧数不足（n_disp×3N < 2阶力常数元数），拟合欠定，虚频不可信"
        print("[WARN] " + note)

    summary = {
        "PHONON_DONE": True,
        "stable": bool(stable),
        "stable_mesh": bool(stable_mesh),
        "mesh_min_frequency_THz": mf,
        "mesh_max_frequency_THz": mx,
        "band_min_frequency_THz": bmf,
        "band_max_frequency_THz": bmx,
        "screening_min_frequency_THz": screen_min,
        "screening_valid": True,
        "screening_status": "valid",
        "stable_new": bool(stable),
        "criterion_version": "phonon-screen-v2",
        "criterion": "finite sampled mesh and band paths; not a whole-BZ proof",
        "min_frequency_THz": screen_min,
        "max_frequency_THz": max(mx, bmx),
        "fit_suspect": bool(fit_suspect),
        "max_freq_lower_bound_THz": bound,
        "assessment_policy": "stable uses min(mesh_min, band_min) >= -0.10 THz; optional max-frequency bound flags fit_suspect; n_disp×3N_sc < nfree_fc2(ALM) flags fit_underdetermined.",
        "n_disp": n_disp,
        "supercell": " ".join(str(x) for x in dim),
        "supercell_atoms": n_sc,
        "nfree_fc2_est": nfree_fc2,
        "fit_frame_margin": frame_margin,
        "fit_underdetermined": fit_underdetermined,
        "fc_cutoff_A": fc_cutoff_A,
        "cutoff_convergence_limitation": "FC_CUTOFF 是计算/建模截断；r_max 不证明 fc2 严格为零，需做 cutoff 收敛性检查。",
        "note": note,
    }
    (cwd / "phonon_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] stable=%s min_freq=%.4f THz max_freq=%.3f THz fit_suspect=%s fit_underdetermined=%s"
          % (str(stable).lower(), mf, mx, str(fit_suspect).lower(),
             str(fit_underdetermined).lower()))


if __name__ == "__main__":
    main()