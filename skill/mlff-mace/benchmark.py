#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""benchmark.py —— step8_benchmark 引擎（在 venv 里跑，需要 mace/torch/ase/phonopy/numpy）。

cwd = 材料目录。跑 §9.2 全部验收闸 + 学习曲线 + 决策表：
    #1 声子谱 RMSE vs DFT fc2（主闸，RMS_MAX）
    #2 imagmodes(pot) == imagmodes(dft)（3D 阈值 -0.1 THz；2D 另有 ZA 闸 #2b）
    #3 平衡结构残余力（微调模型自弛豫后 max|F| < 1e-3 eV/Å，空间群不变）
    #4 ASR 违反量 < 1e-3 eV/Å²
    #5 测试集力 RMSE < 40 meV/Å；#6 测试集能量 RMSE < 3 meV/atom
    #7 弛豫晶格常数 vs DFT < 1%（2D 只看面内）
    #8 EOS（3D: E(V)+体模量；2D: E(面积)+面内二维模量）模量偏差 < 5%、E 曲线 RMSE < 5 meV/atom
    #9 committee σ_F 外推率 < 5%
    #10 模式 Grüneisen 参数 γ(q,ν) MAE < 0.3（±GRUNEISEN_STRAIN 应变 fc2，两边同法）

输出（step8_benchmark/gen-<K>/）：
    validation_summary.json + 5 张图 + results_<材料>.txt + learning_curve.json + plan.json
    （convergence_history.json 由 converge_ctrl.py 追加，顶层同名 validation 由 gen 拷贝）

退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from dataset_build import parse_outcar  # noqa: F401（anchor_energy_shift 等模块级函数用）
except Exception:
    parse_outcar = None
import mace_model as mm  # noqa: E402
import mlff_common as mc  # noqa: E402

EV_A3_TO_GPA = 160.21766208
THZ_TO_CM1 = 33.356


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--mat", required=True)
    p.add_argument("--matdir", default="")          # 材料目录（相对路径的基准）
    p.add_argument("--out", required=True)                  # step8_benchmark/gen-<K>
    p.add_argument("--model", required=True)                # seed-1 模型（发布模型）
    p.add_argument("--seed-dirs", required=True)            # 逗号分隔 seed-* 目录（committee）
    p.add_argument("--train-xyz", required=True)
    p.add_argument("--test-xyz", required=True)
    p.add_argument("--manifest", required=True)             # 当前代顶层清单
    p.add_argument("--step5-dir", default="step5_label")
    p.add_argument("--step1-dir", default="step1_relax")
    p.add_argument("--sc-summary", default="step2_supercell/supercell_summary.json")
    p.add_argument("--dim", required=True)
    p.add_argument("--device", default="cpu")
    p.add_argument("--rmse-max", type=float, default=0.2)
    p.add_argument("--ref-disp", type=float, default=0.1)
    p.add_argument("--grun-strain", type=float, default=0.01)
    p.add_argument("--eos-modulus-tol", type=float, default=0.05)
    p.add_argument("--force-rel-tol", type=float, default=0.03)   # [FIX-相对力判据] #5 相对力误差阈值（默认 3%）
    p.add_argument("--eos-ermse-tol", type=float, default=5.0)     # meV/atom
    p.add_argument("--grun-mae-tol", type=float, default=0.3)
    p.add_argument("--curve-points", default="25,50,100,200,all")
    p.add_argument("--curve-tol", type=float, default=0.05)
    p.add_argument("--foundation", required=True)
    p.add_argument("--replay", default="")
    p.add_argument("--e0s", default="")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--patience", type=int, default=100)
    p.add_argument("--start-swa", type=int, default=1200)
    p.add_argument("--loss", default="huber")
    p.add_argument("--energy-weight", type=float, required=True)
    p.add_argument("--forces-weight", type=float, required=True)
    p.add_argument("--stress-weight", type=float, required=True)
    p.add_argument("--ref-fc2-path", default="")               # 外部 DFT fc2（extend）
    p.add_argument("--kappa-ref", default="")                  # 有 DFT κ 参考时换判据
    return p.parse_args()


# ============================================================ phonopy 小工具
def q_mesh(reps):
    """commensurate q 网格（超胞对应网格）：分数坐标 (i/N) - 0.5 偏移。"""
    pts = []
    for i in range(reps[0]):
        for j in range(reps[1]):
            for k in range(reps[2]):
                pts.append([(i / reps[0]) - 0.5 if i >= reps[0] / 2 else i / reps[0],
                            (j / reps[1]) - 0.5 if j >= reps[1] / 2 else j / reps[1],
                            (k / reps[2]) - 0.5 if k >= reps[2] / 2 else k / reps[2]])
    return np.array(pts)


def make_phonopy(prim_atoms, reps):
    import phonopy
    # primitive_matrix 显式单位阵：输入就是原胞；unitcell 必须是 PhonopyAtoms
    #（phonopy 2.47 不收 ase Atoms）
    return phonopy.Phonopy(unitcell=mm.ase_to_phonopy(prim_atoms),
                           supercell_matrix=np.diag(reps),
                           primitive_matrix=np.eye(3))


def phonopy_from_manifest(prim_atoms, reps, displ_entries, forces, ref_disp):
    """按 step4 清单重建位移集 → phonopy 实例（已设置力）。

    phonopy 的 SVD 求解器不能吃手工拼的约化数据集（rot_map_syms 会越界），所以
    这里对同一原胞重新 `generate_displacements`，按 (disp_number, displacement)
    与清单条目匹配后重排 forces。displ_entries = [{"disp_number","displacement"}]，
    forces 同序 (N, natom, 3)。"""
    ph = make_phonopy(prim_atoms, reps)
    ph.generate_displacements(distance=ref_disp)
    ds = ph.dataset["first_atoms"]
    key_of = {}
    for idx, e in enumerate(displ_entries):
        key = (int(e["disp_number"]),
               tuple(round(float(x), 8) for x in e["displacement"]))
        key_of[key] = idx
    ordered = []
    for d in ds:
        key = (int(d["number"]),
               tuple(round(float(x), 8) for x in d["displacement"]))
        if key not in key_of:
            sys.exit("[ERROR] 清单位移 %s 与 phonopy 重新生成的位移集对不上——\n"
                     "         step4 与 step8 的原胞/应变/REF_DISP 不一致？"
                     % (key,))
        ordered.append(np.array(forces[key_of[key]], dtype=float))
    ph.forces = np.array(ordered)
    ph.produce_force_constants()
    return ph


def phonopy_from_model(prim_atoms, reps, calc, distance):
    """生成位移集 + MACE 取力（扣未位移胞残余力）→ phonopy 实例。返回 (ph, raw_asr, f0max)。"""
    ph = make_phonopy(prim_atoms, reps)
    ph.generate_displacements(distance=distance)
    scells = ph.supercells_with_displacements
    base = mm.phonopy_atoms_to_ase(ph.supercell)
    base.calc = calc
    f0 = np.array(base.get_forces(), dtype=float)
    forces = []
    for sc in scells:
        at = mm.phonopy_atoms_to_ase(sc)
        at.calc = calc
        forces.append(np.array(at.get_forces(), dtype=float) - f0)
    forces = np.array(forces)
    # 原始 fc2 行（单边差分）：ASR 违反量 = max 行声学求和残差
    asr_rows = []
    for e, f in zip(ph.dataset["first_atoms"], forces):
        row = -f / distance          # (natom,3)
        asr_rows.append(float(np.max(np.abs(row.sum(axis=0)))))
    ph.forces = forces
    ph.produce_force_constants()
    return ph, (max(asr_rows) if asr_rows else None), float(np.max(np.linalg.norm(f0, axis=1)))


def freqs_on_mesh(ph, reps, with_eig=False):
    import phonopy
    qs = q_mesh(reps)
    if with_eig:
        ph.run_qpoints(qs, with_eigenvectors=True)
        d = ph.get_qpoints_dict()
        return d["frequencies"], d.get("eigenvectors"), qs
    ph.run_qpoints(qs)
    return ph.get_qpoints_dict()["frequencies"], None, qs


def sorted_freqs(f):
    return np.sort(f, axis=1)


def phonon_rmse(fa, fb):
    return float(np.sqrt(np.mean((sorted_freqs(fa) - sorted_freqs(fb)) ** 2)))


def gruneisen_from_fc2(fneg, fpos, eig_neg, eig_pos, strain, dim):
    """模式 Grüneisen（按 q 点升序带号逐带算，两边同法可直接对比）：
    对每个 q 把 ±ε 两支谱都按频率升序排列（1% 应变下带间交叉罕见，交叉处误差小），
    γ_k = -(1/3)·Δlnω_k/(2ε)（3D）；2D 用 -(1/2)·Δlnω_k/(2ε)。
    过滤：ω<0.05 THz 或 |Δω|>1 THz 的模式不参与（虚频/错配）。
    返回 [(q, k, γ)]——两边按 (q, k) 取交集再比。"""
    out = []
    for q in range(fneg.shape[0]):
        wn = np.sort(fneg[q])
        wp = np.sort(fpos[q])
        for k in range(len(wn)):
            if wn[k] < 0.05 or wp[k] < 0.05 or abs(wn[k] - wp[k]) > 1.0:
                continue
            dln = math.log(max(wp[k], 1e-6)) - math.log(max(wn[k], 1e-6))
            if dim == "2d":
                out.append((int(q), int(k), -dln / (4.0 * strain)))
            else:
                out.append((int(q), int(k), -dln / (6.0 * strain)))
    return out


def gruneisen_mae(g_list_a, g_list_b):
    """两边按 (q, k) 取交集 → γ MAE。返回 (mae, n_common)。"""
    d_b = {(q, k): g for q, k, g in g_list_b}
    common = [(q, k, g) for q, k, g in g_list_a if (q, k) in d_b]
    if not common:
        return None, 0
    mae = float(np.mean([abs(g - d_b[(q, k)]) for q, k, g in common]))
    return mae, len(common)


def band_path(reps):
    """简单可视化路径：Γ → 三个倒格矢顶点 → Γ（诊断图用，闸不用它）。"""
    pts = [[0, 0, 0], [0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5], [0, 0, 0]]
    labels = ["Γ", "X", "Y", "Z", "Γ"]
    nseg = 15
    path, xlabs, x = [], [], []
    for (p0, p1, l0) in zip(pts[:-1], pts[1:], labels[:-1]):
        xlabs.append((sum(x) if x else 0.0, l0))
        for t in range(nseg):
            path.append([p0[c] + (p1[c] - p0[c]) * t / nseg for c in range(3)])
            x.append(len(x) * 1.0)
    xlabs.append((len(x) * 1.0, labels[-1]))
    return np.array(path), xlabs


def strain_prim(prim_atoms, strain, dim, vac_axis):
    """±ε 应变原胞（与 rattle_gen.strain_cell 同法）：3D 三方向 ×(1+ε)；
    2D 仅面内两方向 ×(1+ε)，真空方向长度不变。"""
    at = prim_atoms.copy()
    cell = np.array(at.cell[:], dtype=float)
    if dim == "2d":
        ax = vac_axis if vac_axis in (0, 1, 2) else 2
        for i in range(3):
            if i != ax:
                cell[i] = cell[i] * (1.0 + strain)
    else:
        cell = cell * (1.0 + strain)
    at.set_cell(cell, scale_atoms=True)
    return at


def birch_murnaghan_fit(vols, energies):
    """E(V) = E0 + B0·g(V;V0)（B0'=4 固定），V0 网格 + 线性最小二乘。
    -> (E0, B0_GPa, V0)。

    标准 3 阶 BM（B0'=4）：E = E0 + (9/16) V0 B0 [ x^3·4 + x^2(6 - 4(V0/V)^(2/3)) ]
    代入 (V0/V)^(2/3) = 1+x 化简得 E = E0 + (9/8) V0 B0 x²，其中 x = (V0/V)^(2/3) - 1。
    ★ 旧公式 g=(9/16)V0·x²·(6+4x) 是错的（与标准 BM 差 ~3 倍，体模量被低估 ~3x）。"""
    v0_center = float(np.mean(vols))
    best = None
    for v0 in np.linspace(0.94 * v0_center, 1.06 * v0_center, 80):
        x = (np.array(vols) / v0) ** (-2.0 / 3.0) - 1.0
        g = (9.0 / 8.0) * v0 * x ** 2   # B0'=4 标准 3 阶 BM 化简式
        A = np.vstack([np.ones_like(g), g]).T
        coef, *_ = np.linalg.lstsq(A, np.array(energies), rcond=None)
        rmse = float(np.sqrt(np.mean((A @ coef - np.array(energies)) ** 2)))
        if best is None or rmse < best[3]:
            best = (coef[0], coef[1], v0, rmse)
    return best[0], best[1] * EV_A3_TO_GPA, best[2], best[3]


def anchor_energy_shift(calc, statics, matdir, a):
    """static 帧最小二乘 per-element 能量平移（对齐模型与 DFT 的零点）。"""
    if not statics:
        return {}
    from ase.io import read as _ase_read
    rows = []
    for e in statics:
        at = _ase_read(str(matdir / "step4_genstruct" / ("gen-%d" % e["gen"]) / e["file"]),
                       format="vasp")
        d = parse_outcar(matdir / a.step5_dir / e["id"] / "OUTCAR")
        if d is None:
            continue
        en, _ = eval_model(calc, at)
        cnt = {}
        for sy in at.get_chemical_symbols():
            cnt[sy] = cnt.get(sy, 0) + 1
        rows.append((d["E"] - en, cnt))
    if not rows:
        return {}
    elems = sorted({sy for _, cnt in rows for sy in cnt})
    X = np.array([[cnt.get(sy, 0) for sy in elems] for _, cnt in rows], dtype=float)
    y = np.array([diff for diff, _ in rows], dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {sy: float(c) for sy, c in zip(elems, coef)}


def eval_model(calc, atoms):
    at = atoms.copy()
    at.calc = calc
    e = float(at.get_potential_energy())
    f = np.array(at.get_forces(), dtype=float)
    return e, f


# ============================================================ 主流程
def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    # 材料目录：out = <材料>/step8_benchmark/gen-<K>（相对路径的基准）
    if a.matdir:
        matdir = Path(a.matdir)
    elif Path(a.out).is_absolute():
        matdir = Path(a.out).parent.parent
    else:
        matdir = (Path.cwd() / Path(a.out).parent.parent)
    for _k in ("step1_dir", "step5_dir", "sc_summary", "manifest"):
        _v = getattr(a, _k)
        if _v and not Path(_v).is_absolute():
            setattr(a, _k, str(matdir / _v))
    out = cwd / a.out
    out.mkdir(parents=True, exist_ok=True)

    from ase.io import read as ase_read
    sc_sum = json.loads((cwd / a.sc_summary).read_text(encoding="utf-8"))
    reps = [int(x) for x in sc_sum["supercell_reps"]]
    man = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    # displ/static/iso 帧只在第 0 代生成（gen_step4），后续代的 REF_FC2 / E0s 锚定 /
    # Grüneisen 必须复用 gen-0 的这些帧（当前代清单里只有 rattle 帧）。
    if int(man.get("generation", 0)) > 0:
        _g0 = Path(a.manifest).parent / "gen-0" / "struct_manifest.json"
        if _g0.is_file():
            _m0 = json.loads(_g0.read_text(encoding="utf-8"))
            man["displ_frames"] = _m0.get("displ_frames", [])
            man["frames"] = man.get("frames", []) + [
                e for e in _m0.get("frames", [])
                if e["config_type"] in ("static", "iso")]
    dim = a.dim

    prim_dft = ase_read(str(cwd / a.step1_dir / "CONTCAR"), format="vasp")
    n_prim = len(prim_dft)
    statics = [e for e in man.get("frames", []) if e["config_type"] == "static"]
    gates = []
    t = time.time()

    def gate(name, value, threshold, passed, required=True, note=""):
        gates.append({"name": name, "value": value, "threshold": threshold,
                      "pass": bool(passed), "required": bool(required), "note": note})
        print("[GATE] %-28s %-22s 阈值 %-12s -> %s%s"
              % (name, value, threshold, "PASS" if passed else "FAIL",
                 ("  " + note) if note else ""))

    # ================================================================
    # REF_FC2（DFT）：优先外部 REF_FC2_PATH，否则用 step5 的 displ 帧力拟
    # ================================================================
    from dataset_build import parse_outcar
    if a.ref_fc2_path and Path(a.ref_fc2_path).is_file():
        import phonopy
        ph_dft = phonopy.load(a.ref_fc2_path, produce_fc=True, is_symmetry=True)
        print("[..] REF_FC2 用外部 %s" % a.ref_fc2_path)
    else:
        d0 = [e for e in man.get("displ_frames", []) if abs(e["strain_grun"]) < 1e-9]
        forces = []
        for e in d0:
            d = parse_outcar(cwd / a.step5_dir / e["cfg_id"] / "OUTCAR")
            if d is None:
                sys.exit("[ERROR] displ 帧 %s 的 OUTCAR 没算完" % e["cfg_id"])
            forces.append(d["F"])
        ph_dft = phonopy_from_manifest(prim_dft, reps, d0, forces, a.ref_disp)
    f_dft, _, qs = freqs_on_mesh(ph_dft, reps, with_eig=False)
    f_dft_grun = {}
    for st in (-a.grun_strain, a.grun_strain):
        ds_ = [e for e in man.get("displ_frames", [])
               if abs(e["strain_grun"] - st) < 1e-9]
        prim_s = strain_prim(prim_dft, st, dim, sc_sum.get("vac_axis") or 2)
        forces = [parse_outcar(cwd / a.step5_dir / e["cfg_id"] / "OUTCAR")["F"]
                  for e in ds_]
        f_dft_grun[st] = phonopy_from_manifest(prim_s, reps, ds_, forces, a.ref_disp)
    print("[OK] REF_FC2（DFT）：%.1f s" % (time.time() - t))

    # ================================================================
    # MACE：用微调模型自弛豫 → MACE_FC2（§9.2 核心：势自身的能量极小点）
    # ================================================================
    t = time.time()
    rel_dir = out / "mace_relax"
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / "POSCAR").write_text((cwd / a.step1_dir / "CONTCAR").read_text(),
                                    encoding="utf-8")
    rc = subprocess.run([sys.executable,
                         str(Path(__file__).resolve().parent / "mace_relax.py"),
                         "--model", a.model, "--device", a.device,
                         "--dtype", "float64", "--dim", dim,
                         "--vac-axis", str(sc_sum.get("vac_axis") or 2),
                         "--relax", "true", "--relax-cell", "true",
                         "--fmax", "1e-4", "--steps", "2000", "--opt", "FIRE",
                         "--fix-symmetry", "true", "--symprec", "1e-4",
                         "--cell-policy", "none", "--residual-tol", "1e-3"],
                        cwd=str(rel_dir), capture_output=True, encoding="utf-8")
    rel_log = (rc.stdout or "") + (rc.stderr or "")
    (rel_dir / "benchmark_relax.log").write_text(rel_log, encoding="utf-8")
    rel_sum = mc.read_json(rel_dir / "relax_summary.json", {})
    fmax_r = float(rel_sum.get("max_force_eV_per_A", 99.0))
    sg_in = str(rel_sum.get("spacegroup_in", "")).split("(")[0]
    sg_out = str(rel_sum.get("spacegroup_out", "")).split("(")[0]
    gate("#3 残余力 max|F| < 1e-3", "%.2e eV/Å" % fmax_r, "1e-3 eV/Å",
         fmax_r < 1e-3 and sg_in == sg_out,
         note="空间群 %s→%s" % (sg_in or "?", sg_out or "?"))

    prim_mace = ase_read(str(rel_dir / "CONTCAR"), format="vasp")
    calc, desc = mm.build_calculator(a.model, [cwd], a.device, "float64")
    ph_mace, asr_viol, f0max = phonopy_from_model(prim_mace, reps, calc, a.ref_disp)
    gate("#4 ASR 违反量 < 1e-3", "%.2e eV/Å²" % (asr_viol or 0.0), "1e-3 eV/Å²",
         (asr_viol or 99.0) < 1e-3)
    f_mace, eig_mace, _ = freqs_on_mesh(ph_mace, reps, with_eig=True)
    print("[OK] MACE fc2：%.1f s" % (time.time() - t))

    # ---- #1 / #2 声子对比 ----
    rmse = phonon_rmse(f_dft, f_mace)
    gate("#1 声子谱 RMSE < RMS_MAX", "%.3f THz" % rmse, "%.2f THz" % a.rmse_max,
         rmse < a.rmse_max)
    thr_img = -0.1
    img_dft = bool((f_dft.min() < thr_img))
    img_mace = bool((f_mace.min() < thr_img))
    gate("#2 imagmodes(pot)==imagmodes(dft)", "pot=%s dft=%s" % (img_mace, img_dft),
         "阈值 %.1f THz" % thr_img, img_mace == img_dft,
         note="fmin pot=%.3f dft=%.3f" % (f_mace.min(), f_dft.min()))
    if dim == "2d":
        bmin = 1.0
        q_norms = np.linalg.norm(qs @ np.linalg.inv(np.array(prim_mace.cell[:])), axis=1)
        zone = q_norms < 0.05
        za = float(np.min(f_mace[zone])) if zone.any() else float("nan")
        gate("#2b ZA 支（2D）", "%.3f THz" % za, "≥ -0.05 THz", za >= -0.05,
             note="|q|<0.05|b| 内最低频率")

    # ---- q 点逐点 RMSE 图（下一代定向加采靠它）----
    qmap = {}
    for i in range(f_dft.shape[0]):
        qmap["%.2f,%.2f,%.2f" % tuple(qs[i])] = round(
            float(np.sqrt(np.mean((np.sort(f_dft[i]) - np.sort(f_mace[i])) ** 2))), 3)

    # ================================================================
    # #5/#6 测试集 E/F RMSE（发布模型 seed-1）+ #9 committee σ_F
    # ================================================================
    t = time.time()
    test_atoms = ase_read(a.test_xyz, index=":")
    e_ref = np.array([at.info["REF_energy"] for at in test_atoms])
    f_ref = np.array([at.arrays.get("REF_forces", at.info.get("REF_forces"))
                      for at in test_atoms])
    e_pred, f_pred = [], []
    for at in test_atoms:
        e, f = eval_model(calc, at)
        e_pred.append(e)
        f_pred.append(f)
    e_pred, f_pred = np.array(e_pred), np.array(f_pred)
    f_rmse = float(np.sqrt(np.mean((f_pred - f_ref) ** 2))) * 1000.0      # meV/Å
    e_rmse = float(np.sqrt(np.mean(((e_pred - e_ref) / len(f_ref[0])) ** 2))) * 1000.0
    # [FIX-相对力判据] 绝对 RMSE 受 rattle 大位移帧的力幅值主导，40 meV/Å 阈值对
    # rattle 阶梯数据无判别力。改为相对判据（RMSE / 力模长 RMS），阈值 --force-rel-tol。
    _f_ref_rms = float(np.sqrt(np.mean(f_ref ** 2)))
    _f_diff = f_pred - f_ref
    _rel_all = float(np.sqrt(np.mean(_f_diff ** 2))) / max(_f_ref_rms, 1e-9)
    gate("#5 测试集相对力误差 < %.0f%%" % (a.force_rel_tol * 100),
         "%.1f meV/Å（相对 %.1f%%）" % (f_rmse, _rel_all * 100),
         "%.0f%%" % (a.force_rel_tol * 100), _rel_all < a.force_rel_tol)
    # 分桶：判断误差是"各档均匀（模型问题）"还是"集中在最大幅度档（数据分布问题）"。
    _buckets = {}
    for _at, _fd in zip(test_atoms, _f_diff):
        _vf = float(_at.info.get("volume_factor", 1.0))
        _rs = float(_at.info.get("rattle_std", 0.0))
        _bf = "vf<0.98" if _vf < 0.98 else ("vf>1.02" if _vf > 1.02 else "vf_center")
        _bs = "rs_hi" if _rs > 0.1 else ("rs_mid" if _rs > 0.03 else "rs_lo")
        _k = "%s/%s" % (_bf, _bs)
        _buckets.setdefault(_k, []).append(_fd)
    _rel_parts = ", ".join("%s=%.1f%%" % (k, float(np.sqrt(np.mean(np.array(v) ** 2)))
                                    / max(_f_ref_rms, 1e-9) * 100)
                           for k, v in sorted(_buckets.items()))
    print("[INFO] 相对力误差 RMSE/|F|rms = %.1f%%（分桶：%s）"
          % (_rel_all * 100, _rel_parts))
    # 能量零点：multihead 微调后的模型 E0s 与 DFT 零点差一个常数（estimated 对齐的
    # 是训练内部约定，MACECalculator 输出的绝对能量未必与 DFT 同零点）。用 static
    # 帧最小二乘拟合 per-element 平移量，能量类闸（#6/#8b）都在平移后比较——
    # 力/声子/模量等物理量不受零点影响。
    e_shift = anchor_energy_shift(calc, statics, matdir, a)
    e_pred_s = e_pred + np.array([sum(e_shift.get(s, 0.0) for s in at.get_chemical_symbols())
                                  for at in test_atoms])
    e_rmse_s = float(np.sqrt(np.mean(((e_pred_s - e_ref) / len(f_ref[0])) ** 2))) * 1000.0
    gate("#6 测试集能量 RMSE < 3", "%.2f meV/atom（零点已对齐）" % e_rmse_s,
         "3 meV/atom", e_rmse_s < 3.0,
         note="原始零点 RMSE %.0f meV/atom，per-element shift=%s"
         % (e_rmse, {k: round(v, 4) for k, v in e_shift.items()}))

    # committee
    seed_dirs = [Path(x) for x in a.seed_dirs.split(",") if x.strip()]
    calcs = [mm.build_calculator(str(d / ("%s_gen%d_seed%s.model" % (
        a.mat, a.gen, d.name.split("-")[-1]))), [cwd], a.device, "float64")[0]
        for d in seed_dirs] if seed_dirs else [calc]
    train_atoms = ase_read(a.train_xyz, index=":")[:20]
    sig_train, sig_test = [], []
    for atoms in train_atoms:
        fs = np.array([eval_model(c, atoms)[1] for c in calcs])
        sig_train.append(np.max(np.std(fs, axis=0), axis=1).max())
    for atoms in test_atoms:
        fs = np.array([eval_model(c, atoms)[1] for c in calcs])
        sig_test.append(np.max(np.std(fs, axis=0), axis=1).max())
    med = float(np.median(sig_train))
    extrap = float(np.mean([1.0 if s > 3 * med else 0.0 for s in sig_test]))
    # [FIX-COMMITTEE] #9 改 informational：分母只有 len(test_atoms)≈14 帧，取值只能
    # 是 0/7.1/14.3/21.4…%，5% 阈值等价于「必须 0 帧超阈」= 0% 闸，而 0% 外推率
    # 本身就是「指标失效」的信号（README §9.1 ③）。分辨率与阈值不匹配，不能靠调
    # 阈值救。复活条件（写 TODO）：① 外推率构型集扩到 ≥100（不花 DFT，committee
    # σ_F 只要模型预测，拿未标注 rattle 构型算）；② 换连续量 σ_F 分布 P95/训练中位。
    gate("#9 committee σ_F 外推率 < 5%", "%.1f%%" % (extrap * 100), "5%",
         extrap < 0.05, required=False, note="informational（分母仅 %d 帧，分辨率不足）；训练集中位 σ_F=%.3f。复活条件见 benchmark.py 注释" % (len(sig_test), med))
    print("[OK] 测试集/committee：%.1f s" % (time.time() - t))

    # ================================================================
    # #7 晶格常数 + #8 EOS
    # ================================================================
    t = time.time()
    cell_dft = np.array(prim_dft.cell[:])
    cell_mace = np.array(prim_mace.cell[:])
    devs = [abs(np.linalg.norm(cell_dft[i]) - np.linalg.norm(cell_mace[i]))
            / max(np.linalg.norm(cell_dft[i]), 1e-9) for i in range(3)]
    if dim == "2d":
        devs = devs[:2]
    lat_ok = max(devs) < 0.01
    gate("#7 晶格常数偏差 < 1%", "%.2f%%" % (max(devs) * 100), "1%", lat_ok)

    # EOS：DFT 用 static 帧，MACE 用同结构 + 更细网格
    vols_dft, e_dft = [], []
    statics_ok = []          # [FIX-H1] 只保留 OUTCAR 算完的 static 帧，供 MACE 对齐用
    n_prim = len(prim_dft)
    for e in statics:
        d = parse_outcar(cwd / a.step5_dir / e["id"] / "OUTCAR")
        if d is None:
            print("[WARN] static 帧 %s 没算完，EOS 缺一个点" % e["id"])
            continue
        at = ase_read(str(matdir / "step4_genstruct" / ("gen-%d" % e["gen"]) / e["file"]),
                      format="vasp")
        vols_dft.append(at.get_volume() / len(at))
        e_dft.append(d["E"] / len(at))
        statics_ok.append(e)
    if len(vols_dft) >= 3:
        e0d, b0d, v0d, _ = birch_murnaghan_fit(vols_dft, e_dft)
        # MACE：同结构 + 等体积网格
        vols_m, e_m = [], []
        eos_syms = []
        for e in statics_ok:     # [FIX-H1] 与 vols_dft 同一批帧、同一顺序
            at = ase_read(str(matdir / "step4_genstruct" / ("gen-%d" % e["gen"]) / e["file"]),
                          format="vasp")
            en, _ = eval_model(calc, at)
            vols_m.append(at.get_volume() / len(at))
            e_m.append(en / len(at))
            eos_syms.append(list(at.get_chemical_symbols()))
        for fv in np.linspace(0.96, 1.04, 9):
            at = prim_mace.copy()
            cell = at.get_cell() * (fv ** (1.0 / 3.0))
            at.set_cell(cell, scale_atoms=True)
            en, _ = eval_model(calc, at)
            vols_m.append(at.get_volume() / len(at))
            e_m.append(en / len(at))
            eos_syms.append(list(at.get_chemical_symbols()))
        # [FIX-G3] B0 必须和 DFT 在同一组体积上拟合，否则两个数不可比。
        # 密网格点（linspace 那些）只留作画图/插值，不进 B0 拟合。
        _n_stat = len(statics_ok)
        assert _n_stat == len(vols_dft), "[FIX-H1] static 帧对齐断言失败"
        e0m, b0m, v0m, _ = birch_murnaghan_fit(vols_m[:_n_stat],
                                               e_m[:_n_stat])
        b0_dev = abs(b0m - b0d) / max(abs(b0d), 1e-9)
        # 零点对齐（与 #6 同一个 per-element shift）
        e_m_s = [e + sum(e_shift.get(sy, 0.0) for sy in at_syms) / len(at_syms)
                 for e, at_syms in zip(e_m, eos_syms)]
        _ord = np.argsort(vols_m)
        _vm = [vols_m[i] for i in _ord]
        _em = [e_m_s[i] for i in _ord]
        e_curve_rmse = float(np.sqrt(np.mean(
            (np.interp(vols_dft, _vm, _em) - e_dft) ** 2))) * 1000.0
        # [FIX-seed一致性] B0 对 seed 极敏感：单 seed 达标可能是抽样幸运
        # （sw=100 时 4 seed B0 散布 90.9~110.5，极差 ~20 GPa）。所有 seed 的
        # B0 通过率 < 100% 时 #8a 判 FAIL，避免把单 seed 的抽样当模型性质。
        _seed_b0s = []
        for _d in seed_dirs:
            _sc = mm.build_calculator(str(_d / ("%s_gen%d_seed%s.model" % (
                a.mat, a.gen, _d.name.split("-")[-1]))), [cwd], a.device, "float64")[0]
            _vm2, _em2 = [], []
            for e in statics_ok:
                at = ase_read(str(matdir / "step4_genstruct" / ("gen-%d" % e["gen"]) / e["file"]),
                              format="vasp")
                en, _ = eval_model(_sc, at)
                _vm2.append(at.get_volume() / len(at))
                _em2.append(en / len(at))
            _, _b0s, _, _ = birch_murnaghan_fit(_vm2, _em2)
            _seed_b0s.append(round(_b0s, 1))
        _seed_pass = sum(1 for _b in _seed_b0s
                         if abs(_b - b0d) / max(abs(b0d), 1e-9) < a.eos_modulus_tol)
        _seed_all_ok = (not _seed_b0s) or (_seed_pass == len(_seed_b0s))
        gate("#8a EOS 体模量偏差 < 5%", "DFT %.1f vs MACE %.1f GPa" % (b0d, b0m),
             "5%", b0_dev < a.eos_modulus_tol and _seed_all_ok,
             note="[FIX-seed一致性] %d seed B0=%s，通过 %d/%d（单 seed 达标可能是抽样幸运）"
             % (len(_seed_b0s), ",".join(map(str, _seed_b0s)), _seed_pass, len(_seed_b0s)))
        # [FIX-G4] 绝对阈值在窄应变窗口下是空闸：E(V) 的曲率信号本身
        # ΔE ≈ (9/8)·V0·B0·x²，Si 在 ±3% 处只有 ~5 meV/atom，与阈值 5 同量级——
        # 一条完全平的 E(V) 也能过。改成相对判据：残差 < 曲率信号的 25%。
        _sig = float(np.max(np.abs(np.array(e_dft) - np.min(e_dft)))) * 1000.0
        _rel = e_curve_rmse / max(_sig, 1e-9)
        gate("#8b E 曲线残差 / 曲率信号 < 25%",
             "%.2f / %.2f meV/atom = %.0f%%" % (e_curve_rmse, _sig, _rel * 100),
             "25%", _rel < 0.25,
             note="绝对阈值 %g meV/atom 在窄 VOL_FACTORS 下无判别力，已改相对判据"
                  % a.eos_ermse_tol)
    else:
        gate("#8 EOS", "static 帧不足 %d" % len(vols_dft), "≥3 点", False, required=False,
             note="static 帧没算完，EOS 闸跳过")
    print("[OK] 晶格/EOS：%.1f s" % (time.time() - t))

    # ================================================================
    # #10 Grüneisen（±1% 应变 fc2，两边同法）
    # ================================================================
    t = time.time()
    f_m_grun = {}
    for st in (-a.grun_strain, a.grun_strain):
        prim_s = strain_prim(prim_mace, st, dim, sc_sum.get("vac_axis") or 2)
        ph_s, _, _ = phonopy_from_model(prim_s, reps, calc, a.ref_disp)
        f, eig, _ = freqs_on_mesh(ph_s, reps, with_eig=True)
        f_m_grun[st] = (f, eig)
    f_neg, eig_neg, _ = freqs_on_mesh(f_dft_grun[-a.grun_strain], reps, with_eig=True)
    f_pos, eig_pos, _ = freqs_on_mesh(f_dft_grun[a.grun_strain], reps, with_eig=True)
    g_dft = gruneisen_from_fc2(f_neg, f_pos, eig_neg, eig_pos, a.grun_strain, dim)
    g_mace = gruneisen_from_fc2(f_m_grun[-a.grun_strain][0], f_m_grun[a.grun_strain][0],
                                f_m_grun[-a.grun_strain][1], f_m_grun[a.grun_strain][1],
                                a.grun_strain, dim)
    g_mae, n_common = gruneisen_mae(g_dft, g_mace)
    if n_common:
        gate("#10 Grüneisen γ MAE < 0.3", "%.2f（n=%d）" % (g_mae, n_common),
             "0.3", g_mae < a.grun_mae_tol,
             note="dft γ∈[%.2f,%.2f] mace γ∈[%.2f,%.2f]"
             % (min(x[2] for x in g_dft), max(x[2] for x in g_dft),
                min(x[2] for x in g_mace), max(x[2] for x in g_mace)))
    else:
        gate("#10 Grüneisen", "无有效模式", "MAE<0.3", False, required=False,
             note="±1% 应变 fc2 缺失")
    print("[OK] Grüneisen：%.1f s" % (time.time() - t))

    # ================================================================
    # 图 1/2/5：band_comparison / rmse_phonons / gruneisen_compare
    # ================================================================
    try:
        _plot_bands(f_dft, f_mace, qs, out / ("%s_band_comparison.png" % a.mat))
        _plot_qrmse(qmap, out / ("%s_rmse_phonons.png" % a.mat))
        _d_b = {(q, k): g for q, k, g in g_mace}
        _pairs = [(g, _d_b[(q, k)]) for q, k, g in g_dft if (q, k) in _d_b]
        _plot_grun([p[0] for p in _pairs], [p[1] for p in _pairs],
                   out / "gruneisen_compare.png")
    except Exception as e:
        print("[WARN] 画图失败：%s" % e)

    # ================================================================
    # 学习曲线（不花任何 DFT）
    # ================================================================
    lc_json = out / "curve" / "learning_curve.json"
    if lc_json.is_file():
        # 幂等：学习曲线已存在（改了超参要重跑时删掉 learning_curve.json 即可）
        print("[..] 学习曲线已存在，跳过（%s）" % lc_json)
    else:
        rc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "learning_curve.py"),
             "--gen", str(a.gen), "--mat", a.mat, "--outdir", str(out / "curve"),
             "--train", a.train_xyz, "--test", a.test_xyz,
             "--foundation", a.foundation, "--replay", a.replay or "",
             "--e0s", a.e0s or "", "--device", a.device,
             "--points", a.curve_points, "--tol", str(a.curve_tol),
             "--ref-disp", str(a.ref_disp),
             "--patience", str(a.patience), "--start-swa", str(a.start_swa), "--loss", a.loss,
             "--epochs", str(a.epochs), "--batch-size", str(a.batch_size),
             "--lr", str(a.lr),
             "--energy-weight", str(a.energy_weight),
             "--forces-weight", str(a.forces_weight),
             "--stress-weight", str(a.stress_weight),
             "--ref-freqs", str(out / "ref_freqs.npy"), "--matdir", str(matdir),
             "--dim", dim, "--sc-summary", a.sc_summary,
             "--model", a.model],
            capture_output=True, encoding="utf-8")
        (out / "learning_curve.log").write_text(rc.stdout + rc.stderr, encoding="utf-8")
        print(rc.stdout[-2000:])
        if not lc_json.is_file():
            print("[WARN] 学习曲线没产出 json（rc=%d）" % rc.returncode)

    # ================================================================
    # validation_summary + results.txt + converge_ctrl
    # ================================================================
    results = {
        "mat": a.mat, "generation": a.gen, "dim": dim,
        "model": Path(a.model).name,
        "n_frames_total": man.get("n_frames", 0),
        "phonon_rmse_THz": round(rmse, 4),
        "rmse_max_THz": a.rmse_max,
        "imagmodes_pot": bool(img_mace), "imagmodes_dft": bool(img_dft),
        "test_force_rmse_meV_A": round(f_rmse, 2),
        "test_energy_rmse_meV_atom": round(e_rmse, 2),
        "committee_extrapolation_rate": round(extrap, 4),
        "asr_violation_eV_A2": (asr_viol or 0.0),
        "residual_force_max_eV_A": fmax_r,
        "qpoint_rmse_map": qmap,
        "gates": gates,
        "wall_time_s": round(time.time() - t0, 1),
    }
    val_path = out / "validation_summary.json"
    val_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    # 决策 + 停机
    rc2 = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "converge_ctrl.py"),
         "--gen", str(a.gen), "--validation", str(val_path),
         "--curve", str(lc_json), "--rmse-max", str(a.rmse_max),
         "--improve-min", str(0.02), "--gen-increment", str(20),
         "--max-gen", str(4), "--mat", a.mat,
         "--matdir", str(matdir)],
        capture_output=True, encoding="utf-8")
    print(rc2.stdout)
    if rc2.returncode != 0:
        print(rc2.stderr)
        sys.exit(rc2.returncode)
    val = json.loads(val_path.read_text(encoding="utf-8"))
    status = val.get("status", "expand")

    # 顶层副本（判据 ck_benchmark / step9 读它）
    import shutil
    shutil.copyfile(str(val_path), str(out.parent / "validation_summary.json"))
    print("[OK] validation_summary.json 顶层副本已写")

    # results_<材料>.txt（autoplex 一行摘要）
    hyper = "multihead,f_w=%g,e_w=%g,s_w=%g,lr=%g,%dep,b%s" % (
        a.forces_weight, a.energy_weight, a.stress_weight, a.lr, a.epochs, a.batch_size)
    line = ("MACE-ft    %-10s %-3s %d    %-6.3f          %-8.3f %-10s %-10s %-6d %-8s %s"
            % (a.mat, dim, a.gen, a.ref_disp, rmse,
               str(img_mace), str(img_dft), man.get("n_frames", 0), status, hyper))
    (out / ("results_%s.txt" % a.mat)).write_text(line + "\n", encoding="utf-8", newline="\n")
    print("[DONE] status=%s，总用时 %.1f min" % (status, (time.time() - t0) / 60.0))


# ============================================================ 图
def _plot_bands(f_dft, f_mace, qs, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = np.arange(f_dft.shape[0])
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, sorted_freqs(f_dft), "ko", ms=3, alpha=0.7, label="DFT")
    ax.plot(x, sorted_freqs(f_mace), "r+", ms=4, alpha=0.8, label="MACE")
    ax.set_xlabel("commensurate q 点（超胞网格）")
    ax.set_ylabel("频率 (THz)")
    ax.set_title("声子频率对照（q 网格 %d 点）" % f_dft.shape[0])
    ax.legend()
    fig.tight_layout()
    fig.savefig(str(png_path), dpi=120)
    plt.close(fig)


def _plot_qrmse(qmap, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    vals = sorted(qmap.values())
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(range(len(vals)), vals, width=0.8)
    ax.set_xlabel("q 点（按 RMSE 升序）")
    ax.set_ylabel("RMSE (THz)")
    ax.set_title("q 点逐点声子 RMSE（下一代定向加采依据）")
    fig.tight_layout()
    fig.savefig(str(png_path), dpi=120)
    plt.close(fig)


def _plot_grun(g_dft, g_mace, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.scatter(g_dft, g_mace, s=6, alpha=0.6)
    lo, hi = min(min(g_dft), min(g_mace)), max(max(g_dft), max(g_mace))
    ax.plot([lo, hi], [lo, hi], "k--", lw=1)
    ax.set_xlabel("γ DFT")
    ax.set_ylabel("γ MACE")
    ax.set_title("模式 Grüneisen 对照（n=%d）" % len(g_dft))
    fig.tight_layout()
    fig.savefig(str(png_path), dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
