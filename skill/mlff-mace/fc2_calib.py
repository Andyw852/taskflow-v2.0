#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fc2_calib.py —— step3_calib 引擎（在 venv 里跑，需要 mace/torch/ase/phonopy/numpy）。

用**基座模型**（不是 DFT）在训练超胞上做有限位移拟合 CALIB_FC2，
由它算 300 K 的 RMS 原子位移 u_rms，令 RATTLE_STD = [0.5, 1.0, 1.6] × u_rms。
基座模型定幅度量级足够（差 20% 无所谓），免掉一次 DFT。

⚠️ 两种 fc2 别搞混：
    CALIB_FC2 —— 本脚本，只用来定 rattle 幅度，来自基座模型，免费。
    REF_FC2   —— 验收基准，必须是 DFT（step8 从 step5 的 displ 帧拟出）。

基座模型在本体系有虚频导致 u_rms 算不出来 → 退化到 RATTLE_STD_FALLBACK 并 WARN。
退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mace_model as mm  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--prim", default="step1_relax/CONTCAR")
    p.add_argument("--sc-summary", default="step2_supercell/supercell_summary.json")
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--device", default="cpu")
    p.add_argument("--disp", type=float, default=0.01)
    p.add_argument("--t", type=float, default=300.0)
    p.add_argument("--fallback", default="0.03,0.06,0.10")
    p.add_argument("--out", default="step3_calib/calib_summary.json")
    return p.parse_args()


def main():
    a = parse_args()
    cwd = Path.cwd()
    import phonopy
    from ase.io import read as ase_read

    if not (cwd / a.prim).is_file():
        sys.exit("[ERROR] 找不到 %s" % a.prim)
    sc_sum = json.loads((cwd / a.sc_summary).read_text())
    reps = [int(x) for x in sc_sum["supercell_reps"]]

    prim = ase_read(str(cwd / a.prim), format="vasp")
    calc, desc = mm.build_calculator(a.model, [cwd, cwd.parent, a.model_dir],
                                     a.device, "float64")
    print("[OK] MACE calculator：%s" % desc)

    # ---- phonopy 单原子位移集 + 基座模型取力 ----
    # primitive_matrix 显式给单位阵：输入 CONTCAR 已是原胞，不走 phonopy 的
    # 对称性猜原胞；unitcell 必须是 PhonopyAtoms（2.47 不收 ase Atoms）
    ph = phonopy.Phonopy(unitcell=mm.ase_to_phonopy(prim),
                         supercell_matrix=np.diag(reps),
                         primitive_matrix=np.eye(3))
    ph.generate_displacements(distance=a.disp)
    disp_scs = ph.supercells_with_displacements
    n_disp = len(disp_scs)
    print("[..] CALIB_FC2：%d 个位移超胞（%d 原子/胞）" % (n_disp, len(ph.supercell)))

    base = ph.supercell
    base_ase = mm.phonopy_atoms_to_ase(base)
    base_ase.calc = calc
    f0 = np.array(base_ase.get_forces(), dtype=float)
    f0max = float(np.max(np.linalg.norm(f0, axis=1)))
    print("[..] 未位移超胞残余力 max=%.3e eV/Å（基座模型 vs step1 结构）" % f0max)

    forces = []
    for i, sc in enumerate(disp_scs):
        at = mm.phonopy_atoms_to_ase(sc)
        at.calc = calc
        forces.append(np.array(at.get_forces(), dtype=float) - f0)
        if (i + 1) % 10 == 0 or i + 1 == n_disp:
            print("[..] 取力 %d/%d" % (i + 1, n_disp), flush=True)
    forces = np.array(forces)
    ph.forces = forces
    ph.produce_force_constants()
    fc2 = ph.force_constants
    (cwd / "step3_calib").mkdir(exist_ok=True)
    ph.save(str(cwd / "step3_calib" / "calib_phonopy.yaml"))
    print("[OK] CALIB_FC2 已拟出（%d 帧）" % n_disp)

    # ---- 声子频率 / 虚频检查 / u_rms(300K) ----
    # phonopy 2.47 的 ThermalProperties 不再给 mean_square_displacement，
    # 这里直接用网格本征矢算（与 phonopy 旧实现同一公式）：
    #   u_i²(T) = Σ_{qν} w_q · ℏ/(2 m_i ω) · (2n+1) · |ε_{qν}(i)|²
    ph.run_mesh([8, 8, 8], with_eigenvectors=True)
    mesh = ph.mesh
    freqs = mesh.frequencies                       # (nq, nb) THz
    eig = mesh.eigenvectors                        # (nq, nb, natom*3) 复数
    weights = mesh.weights
    fmin = float(freqs.min())
    print("[..] 基座模型声子频率范围：%.3f ~ %.3f THz" % (fmin, float(freqs.max())))

    summary = {
        "model": desc,
        "n_displacements": int(n_disp),
        "freq_min_THz": round(fmin, 4),
        "residual_force_max_eV_A": f0max,
        "T_K": a.t,
    }

    imag = fmin < -0.1
    if imag:
        rattle = [float(x) for x in a.fallback.split(",") if x.strip()]
        summary.update({
            "imaginary_modes": True,
            "u_rms_A": None,
            "rattle_std_A": rattle,
            "fallback_used": True,
            "warning": ("基座模型在本体系有虚频（fmin=%.3f THz < -0.1），300 K 的 "
                        "u_rms 算不出来，RATTLE_STD 退化到 RATTLE_STD_FALLBACK=%s。"
                        "最终模型的软模行为要重点看 §9.2 的 #2/#2b。"
                        % (fmin, a.fallback)),
        })
        print("[WARN] " + summary["warning"])
    else:
        hbar_ev_s = 6.582119569e-16
        amu_kg = 1.66053906660e-27
        ev_to_j = 1.602176634e-19
        kt_ev = 8.617333262e-5 * a.t
        masses_kg = [m * amu_kg for m in prim.get_masses()]
        natom_prim = len(masses_kg)
        nq, nb = freqs.shape
        cutoff = 1e-3
        wsum = float(weights.sum()) or 1.0
        msd = np.zeros(natom_prim)
        for q in range(nq):
            w = weights[q] / wsum              # Mesh.weights 是重数（和=nq），要归一
            e2 = np.abs(eig[q]) ** 2            # (nb, natom*3)
            for nu in range(nb):
                om = max(float(freqs[q, nu]), cutoff) * 1e12 * 2.0 * math.pi
                x = hbar_ev_s * om / kt_ev
                n_be = 1.0 / (math.exp(x) - 1.0) if x < 700 else 0.0
                pref = hbar_ev_s / (2.0 * om) * (2.0 * n_be + 1.0)   # eV·s²
                for i in range(natom_prim):
                    msd[i] += w * pref / masses_kg[i] * \
                        float(np.sum(e2[nu, 3 * i:3 * i + 3]))
        msd_a2 = msd * ev_to_j * 1.0e20         # eV·s²/kg → J·s²/kg=m² → Å²
        u_rms = float(np.sqrt(np.mean(msd_a2)))
        rattle = [round(0.5 * u_rms, 5), round(1.0 * u_rms, 5), round(1.6 * u_rms, 5)]
        summary.update({
            "imaginary_modes": False,
            "u_rms_A": round(u_rms, 5),
            "mean_square_disp_A2": float(np.mean(msd_a2)),
            "rattle_std_A": rattle,
            "fallback_used": False,
            "warning": None,
        })
        print("[OK] u_rms(300K) = %.5f Å → RATTLE_STD = %s（0.5/1.0/1.6 × u_rms）"
              % (u_rms, rattle))

    out = cwd / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print("[DONE] calib_summary.json")


if __name__ == "__main__":
    main()
