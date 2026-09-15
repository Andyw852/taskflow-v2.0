#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cutoff_conv_driver.py —— FC_CUTOFF 收敛性检查（phonon-mace-cpu 可选步 S5）。

纯后处理：读 step3_phonon 已算好的 POSCAR/disps.npy/forces.npy，对 CUTOFF_LIST
里的每个 cutoff 重新 symfc 拟合 fc2 + run_mesh(60) + auto_band_structure，记下
mesh/band 的最低/最高频率与 screening_min（=min(mesh_min, band_min)），写
cutoff_conv_summary.json。不重算 MACE 力。

converged = 所有 cutoff 下 stable（screening_min >= -0.10）判定一致；
screening_spread_THz = screening_min 的跨-cutoff 极差（越小越收敛）。
"""
import gc
import json
import re
import sys
from pathlib import Path

import numpy as np

# ── 质检阈值（可被 klmace_params.txt 里的同名键覆盖；设为 0 关闭该检查）──
# ① 最高频绝对下限：C60 类材料分子内 C-C 伸缩在 40+ THz。若某 cutoff 下最高频
#    远低于此，说明高频支崩坏（fc2 拟合病态），该 cutoff 的虚频结论不可用。
MAXFREQ_MIN_THZ_DEF = 15.0
# ② 力残差占力 RMS 的百分比警戒线。
RESID_WARN_PCT_DEF = 10.0
# ③ 相对倍率：参数更多（cutoff 更大）理应拟合更好；残差反而超过最优者的该倍数
#    即为病态。
RESID_RATIO_DEF = 1.5


def _nfree_fc2_alm(ph, cutoff_a):
    """给定 cutoff 下的不可约 2 阶力常数元数（= symfc 拟合的未知数）。

    方程数 n_disp × 3N_sc 小于它即为欠定，此时虚频不可信。与
    phonon_fit_driver._nfree_fc2_alm 同源；缺 alm 或结构异常时返回 None
    （只记 null，不阻塞拟合）。
    """
    try:
        from ase import Atoms
        from alm import ALM
    except Exception:
        return None
    try:
        sc = ph.supercell
        atoms = Atoms(numbers=sc.numbers, positions=sc.positions,
                      cell=sc.cell, pbc=True)
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


def _read_params(path):
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
    from phonopy import Phonopy
    from phonopy.interface.vasp import read_vasp

    cwd = Path.cwd()
    for f in ("POSCAR", "disps.npy", "forces.npy"):
        if not (cwd / f).is_file():
            sys.exit("[ERROR] 缺 %s（step3 数据没拉全？）" % f)

    uc = read_vasp("POSCAR")
    params = _read_params(cwd / "klmace_params.txt")
    _dim = np.array([int(x) for x in (params.get("SUPERCELL") or "1 1 1").split()],
                    dtype=int)
    # 3 个数=对角；9 个数=3×3 矩阵（行主序，phono3py --dim 同义）；np.prod 对矩阵=行列式
    dim = _dim.reshape(3, 3) if _dim.size == 9 else np.diag(_dim)
    raw_list = str(params.get("CUTOFF_LIST") or "4.0,6.0,8.0")
    cutoffs = [float(x) for x in raw_list.replace(" ", "").split(",") if x.strip()]
    if not cutoffs:
        sys.exit("[ERROR] CUTOFF_LIST 为空")
    maxfreq_min = float(params.get("MAXFREQ_MIN_THZ") or MAXFREQ_MIN_THZ_DEF)
    resid_warn = float(params.get("RESID_WARN_PCT") or RESID_WARN_PCT_DEF)
    resid_ratio = float(params.get("RESID_RATIO") or RESID_RATIO_DEF)

    disps = np.ascontiguousarray(np.load("disps.npy"), dtype="double")
    forces = np.ascontiguousarray(np.load("forces.npy"), dtype="double")
    n_disp = int(len(disps))
    n_sc = int(len(uc.numbers) * int(np.prod(dim)))
    print("[..] %d 帧 × %d 原子，cutoff 序列=%s" % (n_disp, n_sc, cutoffs))

    RX = re.compile(r"^\s*frequency:\s*([^\s#]+)", re.MULTILINE)
    results = []
    for cut in cutoffs:
        try:
            ph = Phonopy(uc, supercell_matrix=np.array(dim, dtype=int),
                         primitive_matrix="P")
            ph.dataset = {"displacements": disps, "forces": forces}
            ph.produce_force_constants(
                fc_calculator="symfc",
                fc_calculator_options="cutoff = %g" % cut,
            )
            # 质检①：力残差——用拟合出的 fc2 反推力，与 MACE 力比较。
            # 残差占力 RMS 的比例是判断"拟合是否可信"的第一手证据：真拟合
            # 应能复现绝大部分力；崩坏/病态的拟合残差会显著偏大。
            _fc = np.asarray(ph.force_constants)
            _pred = -np.einsum("ijab,ujb->uia", _fc, disps)
            resid_rms = float(np.sqrt(np.mean((_pred - forces) ** 2)))
            force_rms = float(np.sqrt(np.mean(forces ** 2)))
            resid_pct = (100.0 * resid_rms / force_rms) if force_rms > 0 else None
            # 质检②：位移帧数是否足以确定 fc2（方程数 vs 未知数）。
            nfree = _nfree_fc2_alm(ph, cut)
            ndof = int(n_disp * 3 * n_sc)
            frame_margin = (round(ndof / float(nfree), 3)
                            if (nfree and nfree > 0) else None)
            underdet = (bool(ndof < nfree) if (nfree and nfree > 0) else None)
            ph.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
            mfreqs = np.asarray(ph.get_mesh_dict()["frequencies"], dtype=float)
            mf, mx = float(mfreqs.min()), float(mfreqs.max())
            band_name = "band-cut%g.yaml" % cut
            ph.auto_band_structure(plot=False, write_yaml=True, filename=band_name)
            bt = (cwd / band_name).read_text(encoding="utf-8", errors="replace")
            bv = np.asarray([float(x) for x in RX.findall(bt)], dtype=float)
            bmf, bmx = float(bv.min()), float(bv.max())
            screen = min(mf, bmf)
            results.append({
                "cutoff_A": round(cut, 3),
                "mesh_min_THz": round(mf, 6),
                "mesh_max_THz": round(mx, 6),
                "band_min_THz": round(bmf, 6),
                "band_max_THz": round(bmx, 6),
                "screening_min_THz": round(screen, 6),
                "stable": bool(screen >= -0.10),
                "fit_resid_rms": round(resid_rms, 6),
                "force_rms": round(force_rms, 6),
                "fit_resid_pct": (round(resid_pct, 4) if resid_pct is not None else None),
                "nfree_fc2": nfree,
                "fit_frame_margin": frame_margin,
                "fit_underdetermined": underdet,
            })
            print("[OK] cutoff=%-4g mesh_min=%+.4f band_min=%+.4f screen=%+.4f %s"
                  % (cut, mf, bmf, screen,
                     "stable" if screen >= -0.10 else "unstable"))
        except Exception as e:
            results.append({"cutoff_A": round(cut, 3), "error": str(e)})
            print("[FAIL] cutoff=%-4g %s" % (cut, e))
        finally:
            try:
                del ph
            except Exception:
                pass
            gc.collect()

    good = [r for r in results if "error" not in r]
    verdicts = [r["stable"] for r in good]
    spread = (max(r["screening_min_THz"] for r in good)
              - min(r["screening_min_THz"] for r in good)) if good else None
    converged = bool(good) and len(set(verdicts)) == 1

    # ── 质检③：跨 cutoff 的高频支崩坏 + 力残差异常 ──
    # 判据不是"稳/不稳"，而是"这次拟合本身可不可信"。任何一项不过，对应
    # cutoff 的 screening_min 都不能当作物理结论使用。
    _maxes = [r["mesh_max_THz"] for r in good
              if isinstance(r.get("mesh_max_THz"), (int, float))]
    med_max = float(np.median(_maxes)) if _maxes else None
    _resids = [r["fit_resid_pct"] for r in good
               if isinstance(r.get("fit_resid_pct"), (int, float))]
    best_resid = min(_resids) if _resids else None
    flags = []
    for r in good:
        c = r["cutoff_A"]
        mx = r.get("mesh_max_THz")
        rel = bool(med_max and med_max > 0 and mx is not None and mx < 0.5 * med_max)
        ab = bool(maxfreq_min > 0 and mx is not None and mx < maxfreq_min)
        r["freq_collapse"] = (bool(rel or ab) if mx is not None else None)
        if r["freq_collapse"]:
            flags.append("cut%g:高频支崩坏(max=%+.2f THz)" % (c, mx))
        if r.get("fit_underdetermined"):
            flags.append("cut%g:拟合欠定(未知数>方程数)" % c)
        p = r.get("fit_resid_pct")
        if isinstance(p, (int, float)):
            if resid_warn > 0 and p > resid_warn:
                flags.append("cut%g:力残差偏大(%.1f%%)" % (c, p))
            elif best_resid and resid_ratio > 0 and p > resid_ratio * best_resid:
                flags.append("cut%g:力残差异常(%.1f%% vs 最优 %.1f%%)"
                             % (c, p, best_resid))
    for r in results:
        if "error" in r:
            flags.append("cut%g:拟合失败(%s)" % (r["cutoff_A"], r["error"]))
    quality_ok = not flags

    summary = {
        "CUTOFF_CONV_DONE": True,
        "n_disp": n_disp,
        "supercell": " ".join(str(x) for x in dim),
        "supercell_atoms": n_sc,
        "cutoffs_A": cutoffs,
        "results": results,
        "converged": converged,
        "screening_spread_THz": round(spread, 6) if spread is not None else None,
        "quality_ok": quality_ok,
        "quality_flags": flags,
        "max_freq_median_THz": (round(med_max, 6) if med_max is not None else None),
        "fit_resid_best_pct": (round(best_resid, 4) if best_resid is not None else None),
        "note": "converged=True 表示各 cutoff 下 stable(screening_min>=-0.10) 判定一致；"
                "spread 是 screening_min 的跨-cutoff 极差。"
                "quality_ok=False 时（见 quality_flags）该次拟合不可信，screening_min "
                "不能当物理结论用：fit_resid_pct 是力残差占力 RMS 的百分比（反推力度量），"
                "fit_frame_margin=n_disp×3N_sc/nfree_fc2 是方程数/未知数余量"
                "（<1 即 fit_underdetermined），freq_collapse 表示最高频远低于其它 "
                "cutoff（高频支崩坏）。error 项表示该 cutoff 拟合失败。",
    }
    (cwd / "cutoff_conv_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] cutoff_conv converged=%s spread=%s"
          % (str(converged).lower(),
             ("%.4f THz" % spread) if spread is not None else "n/a"))


if __name__ == "__main__":
    main()
