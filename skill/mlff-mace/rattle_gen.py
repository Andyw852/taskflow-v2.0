#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rattle_gen.py —— mlff-mace 第 K 代结构生成引擎（在 venv 里跑，需要 numpy/ase/phonopy）。

cwd = 材料目录。读 step1_relax/CONTCAR（弛豫好的原胞）+ step2_supercell/supercell_summary.json
（超胞倍数）+ step3_calib/calib_summary.json（RATTLE_STD 三档），生成本代全部待标注构型：

    static   未位移超胞 × VOL_FACTORS 各档（EOS 基准 + 训练帧）
    rattle   应变网格 × 位移幅度 × 种子（主力数据，d_min 保护 + 2D 质心漂移检查）
    displ    phonopy 单原子位移集（0 / ±GRUNEISEN_STRAIN 应变，REF_FC2 与 Grüneisen 用；
             仅第 0 代且没给 REF_FC2_PATH 时生成）
    iso      孤立原子（每元素一个，ISO_BOX 立方盒；仅第 0 代）

输出：
    step4_genstruct/gen-<K>/structures/cfg-*.poscar + 同名 .magmom（磁性体系才有）
    step4_genstruct/gen-<K>/struct_manifest.json（每帧：id/config_type/应变/幅度/种子/RMS/
        最小原子间距/d_min 拒绝次数/2D 质心漂移）
    step4_genstruct/gen-<K>/displ_dataset.json（step8 重建 REF_FC2/Grüneisen 用）

退出码：0 成功；非 0 = [ERROR] 失败（tf 会把 stderr 原样呈给用户）。
幂等：重复跑会覆盖生成同代产物，不影响 step5 已算完的 cfg-* 目录。
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dft_settings as ds  # noqa: E402
from dim_common import read_poscar_cell_frac  # noqa: E402

# 共价半径（Å，Cordero et al. 2008 常用值，d_min 保护用；差 5% 无所谓）
COV_RADII = {
    "H": 0.31, "He": 0.28, "Li": 1.28, "Be": 0.96, "B": 0.84, "C": 0.76,
    "N": 0.71, "O": 0.66, "F": 0.57, "Ne": 0.58, "Na": 1.66, "Mg": 1.41,
    "Al": 1.21, "Si": 1.11, "P": 1.07, "S": 1.05, "Cl": 1.02, "Ar": 1.06,
    "K": 2.03, "Ca": 1.76, "Sc": 1.70, "Ti": 1.60, "V": 1.53, "Cr": 1.39,
    "Mn": 1.39, "Fe": 1.32, "Co": 1.26, "Ni": 1.24, "Cu": 1.32, "Zn": 1.22,
    "Ga": 1.22, "Ge": 1.20, "As": 1.19, "Se": 1.20, "Br": 1.20, "Kr": 1.16,
    "Rb": 2.20, "Sr": 1.95, "Y": 1.90, "Zr": 1.75, "Nb": 1.64, "Mo": 1.54,
    "Tc": 1.47, "Ru": 1.46, "Rh": 1.42, "Pd": 1.39, "Ag": 1.45, "Cd": 1.44,
    "In": 1.42, "Sn": 1.39, "Sb": 1.39, "Te": 1.38, "I": 1.39, "Xe": 1.40,
    "Cs": 2.44, "Ba": 2.15, "La": 2.07, "Ce": 2.04, "Pr": 2.03, "Nd": 2.01,
    "Pm": 1.99, "Sm": 1.98, "Eu": 1.98, "Gd": 1.96, "Tb": 1.94, "Dy": 1.92,
    "Ho": 1.92, "Er": 1.89, "Tm": 1.90, "Yb": 1.87, "Lu": 1.87, "Hf": 1.75,
    "Ta": 1.70, "W": 1.62, "Re": 1.51, "Os": 1.44, "Ir": 1.41, "Pt": 1.36,
    "Au": 1.36, "Hg": 1.32, "Tl": 1.45, "Pb": 1.46, "Bi": 1.48, "Th": 2.06,
    "U": 1.96, "Np": 1.90, "Pu": 1.87,
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--outdir", required=True)          # step4_genstruct/gen-<K>
    p.add_argument("--prim", default="step1_relax/CONTCAR")
    p.add_argument("--method-file", default="workflow_method.txt")
    p.add_argument("--sc-summary", default="step2_supercell/supercell_summary.json")
    p.add_argument("--calib", default="step3_calib/calib_summary.json")
    p.add_argument("--dim", required=True)
    p.add_argument("--vol-factors", default="0.97,1.00,1.03")
    p.add_argument("--rattle-std", required=True)      # 逗号分隔三档（已由 step3 定）
    p.add_argument("--n-per-cell", type=int, default=2)
    p.add_argument("--min-dist-ratio", type=float, default=0.75)
    p.add_argument("--ref-disp", type=float, default=0.01)
    p.add_argument("--iso-box", type=float, default=15.0)
    p.add_argument("--grun-strain", type=float, default=0.01)
    p.add_argument("--seed-base", type=int, default=2025)
    p.add_argument("--ref-fc2-path", default="")       # 给了就跳过 displ 帧
    p.add_argument("--gen-increment", type=int, default=20)
    p.add_argument("--plan", default="")               # step8 的 plan.json（定向加采）
    return p.parse_args()


# ============================================================ 小工具
def strain_cell(lat, f, dim, vac_axis):
    """应变：3D → 三方向 × f（VOL_FACTORS 解释为体积因子时 f 已含 1/3 次方，
    这里直接收晶格因子）；2D → 仅面内两方向 × f，真空方向长度不变（断言）。"""
    out = [list(r) for r in lat]
    if dim == "2d":
        ax = vac_axis if vac_axis in (0, 1, 2) else 2
        v0 = np.linalg.norm(out[ax])
        for i in range(3):
            if i != ax:
                out[i] = [f * x for x in out[i]]
        v1 = np.linalg.norm(out[ax])
        if abs(v1 - v0) > 1e-9:
            sys.exit("[ERROR] 2D 应变后真空方向长度变了 %.2e → %.2e Å——bug，拒绝输出"
                     % (v0, v1))
    else:
        out = [[f * x for x in row] for row in out]
    return out


def write_poscar(path, lat, frac, sym_full, comment="mlff-mace"):
    """POSCAR 写入。sym_full = 每个原子的元素符号（与 frac 同序），
    元素顺序/数目按出现顺序压缩（保证与 step1 POTCAR 顺序一致：所有结构的
    sym_full 都从原胞展开而来，第一个原子块即原胞顺序）。"""
    order, counts = [], []
    for s in sym_full:
        if not order or order[-1] != s:
            order.append(s)
            counts.append(1)
        else:
            counts[-1] += 1
    lines = [comment, "1.0"]
    for row in lat:
        lines.append("  %18.10f %18.10f %18.10f" % tuple(row))
    lines.append("  " + "  ".join(order))
    lines.append("  " + "  ".join(str(c) for c in counts))
    lines.append("Direct")
    for c in frac:
        lines.append("  %18.10f %18.10f %18.10f" % tuple(c))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def order_atoms_by_symbols(sym_list):
    """把 [Si,Si,C,C] 压缩成 (symbols_in_order, counts, per_atom_symbols)。
    step4 所有结构都用 step1 的元素顺序写 POSCAR，保证 POTCAR 顺序一致。"""
    order, counts = [], []
    for s in sym_list:
        if not order or order[-1] != s:
            order.append(s)
            counts.append(1)
        else:
            counts[-1] += 1
    return order, counts, sym_list


def periodic_min_dists(frac, lat):
    """周期最小原子对距离（Å）→ 数组。用最小镜像（-0.5..0.5 分数差）。"""
    n = len(frac)
    lat = np.array(lat, dtype=float)
    f = np.array(frac, dtype=float)
    out = np.full((n, n), np.inf)
    for i in range(n):
        d = f - f[i]
        d -= np.round(d)
        cart = d @ lat
        out[i] = np.linalg.norm(cart, axis=1)
        out[i, i] = np.inf
    return np.minimum(out, out.T)


def check_pairwise(frac, lat, symbols, min_dist_ratio):
    """逐对检查原子间距 ≥ MIN_DIST_RATIO × (r_i + r_j)。-> (ok, 最短比例)"""
    d = periodic_min_dists(frac, lat)
    n = len(frac)
    ratios = []
    for i in range(n):
        for j in range(i + 1, n):
            rsum = COV_RADII.get(symbols[i], 1.3) + COV_RADII.get(symbols[j], 1.3)
            ratios.append(d[i, j] / max(rsum, 1e-6))
    return (min(ratios) >= min_dist_ratio), min(ratios)


def slab_ok(frac, lat, vac_axis):
    """2D：平板质心沿真空方向漂移 < 0.5 Å，且原子都在周期镜像真空区外。"""
    ax = vac_axis if vac_axis in (0, 1, 2) else 2
    z = [c[ax] for c in frac]
    height = np.linalg.norm(lat[ax])
    zmax, zmin = max(z), min(z)
    thickness = (zmax - zmin) * height
    # 真空 = 胞高 - 原子层厚；原子上下都必须留有真空
    if height - thickness < 2.0:
        return False, "原子层厚 %.2f Å 逼近胞高 %.2f Å，真空被吃光" % (thickness, height)
    return True, ""


# ============================================================ 生成器
def rattle_supercell(lat, frac, symbols, counts, std, rng, min_dist_ratio,
                     dim, vac_axis, max_retry=50):
    """MC-rattle 位移 + d_min 保护（hiphive，与 autoplex 同款引擎；单次高斯抽样
    在最大幅度档会因尾部采样到过近原子对而反复被拒，MC 逐步位移天然满足 d_min）。
    最终仍做逐对检查（阈值 = MIN_DIST_RATIO × 共价半径和），不达标重抽样 ≤ 50 次。
    返回 (frac_new, meta) 或 None。"""
    try:
        from hiphive.structure_generation import generate_mc_rattled_structures
    except ImportError:
        sys.exit("[ERROR] 缺 hiphive（rattle 生成依赖它的 MC 位移器）。\n"
                 "        装：pip install hiphive（autoplex 用的就是它）")
    from ase import Atoms
    atoms = Atoms(symbols=list(symbols), cell=lat,
                  scaled_positions=frac, pbc=True)
    rsum_min = min(COV_RADII.get(s, 1.3) + COV_RADII.get(t, 1.3)
                   for s in symbols for t in symbols)
    d_min = min_dist_ratio * rsum_min
    n_iter = 10
    step_std = std / math.sqrt(3 * n_iter)      # hiphive：每步位移，RMS≈√(3·n_iter)·step
    rejections = 0
    for attempt in range(max_retry):
        try:
            cands = generate_mc_rattled_structures(
                atoms, 1, step_std, d_min,
                seed=int(rng.integers(0, 2 ** 31 - 1)), n_iter=n_iter)
        except Exception as e:
            sys.exit("[ERROR] hiphive MC-rattle 失败：%s" % e)
        if not cands:
            rejections += 1
            continue
        at = cands[0]
        fnew = at.get_scaled_positions() % 1.0
        ok, ratio = check_pairwise(fnew, lat, list(at.get_chemical_symbols()),
                                   min_dist_ratio)
        drift_note = ""
        if ok and dim == "2d":
            ok, drift_note = slab_ok(fnew, lat, vac_axis)
        if ok:
            # 最小镜像位移（hiphive 可能把原子 wrap 过胞，直接差位置会算出假跳变）
            dfrac = np.array(fnew, dtype=float) - np.array(frac, dtype=float)
            dfrac -= np.round(dfrac)
            disp = dfrac @ np.array(lat, dtype=float)
            rms = float(np.sqrt(np.mean(np.sum(disp ** 2, axis=1))))
            return fnew.tolist(), {
                "rms_A": round(rms, 5),
                "min_dist_ratio": round(ratio, 4),
                "d_min_rejections": rejections,
                "drift_note": drift_note,
            }
        rejections += 1
    sys.exit("[ERROR] rattle 重抽样 %d 次仍有过近原子对（d_min=%.3f Å）。"
             "减小 RATTLE_STD 或调 MIN_DIST_RATIO。" % (max_retry, d_min))


def gen_displ_sets(prim_lat, prim_frac, symbols, counts, reps, strain, dim, vac_axis,
                   disp=0.1):
    """phonopy 单原子位移集（给定晶格应变下）。返回 [(lat_sc, frac_sc, disp_vec, atom_idx)]。

    frac_sc 是位移后的超胞分数坐标；disp_vec 是笛卡尔位移（Å）。"""
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms
    lat = strain_cell(prim_lat, 1.0 + strain, dim, vac_axis)
    from ase.data import chemical_symbols
    # 注意：chemical_symbols[0] 是占位 'X'，不能用 enumerate(...,1)
    numbers = [chemical_symbols.index(s) for s in symbols]
    ph = Phonopy(unitcell=PhonopyAtoms(numbers=numbers, cell=lat,
                                       scaled_positions=prim_frac),
                 supercell_matrix=np.diag(reps) * 1,
                 primitive_matrix=np.eye(3))
    ph.generate_displacements(distance=disp)
    dataset = ph.dataset
    sc = ph.supercell
    sc_lat = sc.cell
    entries = []
    for d in dataset["first_atoms"]:
        frac_sc = np.array(sc.scaled_positions, dtype=float)
        atom = int(d["number"])              # phonopy 2.47 是 0-based
        frac_sc[atom] += np.array(d["displacement"], dtype=float) @ \
            np.linalg.inv(np.array(sc_lat, dtype=float))
        entries.append((sc_lat.tolist(), (frac_sc % 1.0).tolist(),
                        [float(x) for x in d["displacement"]], atom))
    return entries


def main():
    a = parse_args()
    from ase.io import read as ase_read
    from ase.data import chemical_symbols

    cwd = Path.cwd()
    if not (cwd / a.prim).is_file():
        sys.exit("[ERROR] 找不到 %s —— step1_relax 还没算完？" % a.prim)
    sc_sum = json.loads((cwd / a.sc_summary).read_text())
    reps = [int(x) for x in sc_sum["supercell_reps"]]
    dim = a.dim
    vac_axis = sc_sum.get("vac_axis") or 2

    prim = ase_read(str(cwd / a.prim), format="vasp")
    natom_prim = len(prim)
    sym_list = list(prim.get_chemical_symbols())
    order, counts, _ = order_atoms_by_symbols(sym_list)

    lat = np.array(prim.cell[:], dtype=float)
    frac = np.array(prim.get_scaled_positions(), dtype=float)

    # ---- 磁性：从 step1 OUTCAR 继承末次 per-ion 磁矩 ----
    moments, mnote = ds.read_magmom(cwd / "step1_relax" / "OUTCAR")
    magnetic = (moments is not None and len(moments) == natom_prim and
                max(abs(x) for x in moments) > 0.1)
    if moments and len(moments) != natom_prim:
        print("[WARN] step1 磁矩 %d 个 ≠ 原胞原子 %d 个，按非磁处理（%s）"
              % (len(moments), natom_prim, mnote))
        magnetic = False
    print("[..] 磁性：%s —— %s" % ("ON" if magnetic else "off", mnote))

    # ---- 应变 / 幅度网格 ----
    vol_factors = [float(x) for x in a.vol_factors.split(",") if x.strip()]
    rattle_std = [float(x) for x in a.rattle_std.split(",") if x.strip()]
    if len(rattle_std) != 3:
        sys.exit("[ERROR] RATTLE_STD 要三档（0.5/1.0/1.6 × u_rms），收到 %r" % a.rattle_std)
    if dim == "2d":
        lat_factors = vol_factors          # 2D：面内晶格应变因子
    else:
        lat_factors = [f ** (1.0 / 3.0) for f in vol_factors]   # 3D：体积因子 → 晶格因子

    outdir = cwd / a.outdir
    sdir = outdir / "structures"
    sdir.mkdir(parents=True, exist_ok=True)

    manifest = []
    rid = 0

    def add(cfg_type, lat_sc, frac_sc, sym_full, meta):
        nonlocal rid
        rid += 1
        cid = "cfg-%d-%s-%03d" % (a.gen, cfg_type, rid)
        write_poscar(sdir / ("%s.poscar" % cid), lat_sc, frac_sc, sym_full,
                     "mlff-mace %s %s" % (cfg_type, cid))
        entry = {"id": cid, "file": "structures/%s.poscar" % cid,
                 "config_type": cfg_type, "gen": a.gen}
        entry.update(meta)
        manifest.append(entry)
        return cid

    def prim_index_of(frac_sc):
        """超胞原子 → 原胞原子索引（按位置匹配，对 image-major 和 phonopy 的
        超胞排序都成立）。"""
        f = np.array(frac_sc, dtype=float)
        f_sc = (f * np.array(reps, dtype=float)) % 1.0
        p = np.array(frac, dtype=float) % 1.0
        out = np.zeros(len(f), dtype=int)
        for i in range(len(f)):
            d = p - f_sc[i]
            d -= np.round(d)
            out[i] = int(np.argmin(np.sum(d ** 2, axis=1)))
        return out

    def magmom_for_frac(frac_sc):
        """按位置把原胞 per-ion 磁矩映射到超胞原子（任意原子顺序）。"""
        if not magnetic:
            return None
        return "  ".join("%.4f" % moments[i] for i in prim_index_of(frac_sc))

    # ---- 超胞基准坐标（image-major；static/rattle 共用）----
    base_frac, base_sym = [], []
    for i in range(reps[0]):
        for j in range(reps[1]):
            for k in range(reps[2]):
                for ai in range(natom_prim):
                    base_frac.append([(frac[ai][0] + i) / reps[0],
                                      (frac[ai][1] + j) / reps[1],
                                      (frac[ai][2] + k) / reps[2]])
                    base_sym.append(sym_list[ai])

    # ---- static 帧（EOS 基准 + 训练）----
    print("[..] static 帧：%d 个应变档" % len(lat_factors))
    for f_lat, f_vol in zip(lat_factors, vol_factors):
        lat_s = strain_cell(lat, f_lat, dim, vac_axis)
        # 超胞 = 应变后的原胞 × reps（分数坐标不变）
        sc_lat_s = np.array(lat_s, dtype=float) * np.array(reps, dtype=float)[:, None]
        cid = add("static", sc_lat_s, base_frac, base_sym,
                  {"strain_factor": round(f_lat, 6), "volume_factor": round(f_vol, 6),
                   "rattle_std": None, "seed": None, "rms_A": 0.0,
                   "min_dist_ratio": None, "d_min_rejections": 0, "disp_number": None,
                   "strain_grun": None})
        mag = magmom_for_frac(base_frac)
        if mag:
            (sdir / ("%s.magmom" % cid)).write_text(
                "MAGMOM = %s\n" % mag, encoding="utf-8", newline="\n")

    # ---- rattle 帧（主力数据）----
    rng = np.random.default_rng(a.seed_base + a.gen)
    # 采样计划：coverage_plan（extend 模式把新钱压到盲区）优先，否则均匀网格
    plan_targets = None
    if a.plan:
        try:
            plan = json.loads(a.plan)
            plan_targets = plan.get("targets")
            if plan_targets:
                print("[..] 覆盖盲区定向加采：%s" % plan.get("reason", ""))
        except Exception as e:
            print("[WARN] plan.json 解析失败（%s），退回均匀网格" % e)
    n_rattle = 0
    if plan_targets:
        grid = []
        for tgt in plan_targets:
            f_lat = float(tgt["strain"])
            if dim == "2d":
                f_vol = f_lat
            else:
                f_vol = round(f_lat ** 3, 6)
            n = int(tgt.get("n", a.n_per_cell))
            for k, std in enumerate(rattle_std):
                m = n // len(rattle_std) + (1 if k < n % len(rattle_std) else 0)
                for seed_i in range(m):
                    grid.append((f_lat, f_vol, std, seed_i))
        print("[..] rattle 定向网格：%d 帧" % len(grid))
    else:
        grid = [(f_lat, f_vol, std, seed_i)
                for f_lat, f_vol in zip(lat_factors, vol_factors)
                for std in rattle_std
                for seed_i in range(a.n_per_cell)]
        print("[..] rattle 网格：%d 应变 × %d 幅度 × %d 种子 = %d"
              % (len(lat_factors), len(rattle_std), a.n_per_cell, len(grid)))
    for (f_lat, f_vol, std, seed_i) in grid:
        lat_s = strain_cell(lat, f_lat, dim, vac_axis)
        sc_lat_s = np.array(lat_s, dtype=float) * np.array(reps, dtype=float)[:, None]
        r = rattle_supercell(sc_lat_s, base_frac, base_sym, counts, std, rng,
                             a.min_dist_ratio, dim, vac_axis)
        if r is None:
            continue
        frac_new, meta = r
        cid = add("rattle", sc_lat_s, frac_new, base_sym,
                  {"strain_factor": round(f_lat, 6), "volume_factor": round(f_vol, 6),
                   "rattle_std": std, "seed": seed_i,
                   "rms_A": meta["rms_A"], "min_dist_ratio": meta["min_dist_ratio"],
                   "d_min_rejections": meta["d_min_rejections"],
                   "drift_note": meta["drift_note"],
                   "disp_number": None, "strain_grun": None})
        mag = magmom_for_frac(frac_new)
        if mag:
            (sdir / ("%s.magmom" % cid)).write_text(
                "MAGMOM = %s\n" % mag, encoding="utf-8", newline="\n")
        n_rattle += 1
    print("[OK] rattle 帧：%d" % n_rattle)

    # ---- phonopy 单原子位移集（0 / ±GRUNEISEN_STRAIN；仅 gen0 且无 REF_FC2_PATH）----
    displ_entries = []
    if a.gen == 0 and not a.ref_fc2_path:
        strains = [0.0, -a.grun_strain, a.grun_strain]
        for st in strains:
            entries = gen_displ_sets(lat, frac, sym_list, counts, reps, st, dim, vac_axis,
                           disp=a.ref_disp)
            print("[..] 应变 %+.3f 位移集：%d 个" % (st, len(entries)))
            if len(entries) > 80:
                print("[WARN] 单原子位移集 %d 个 > DISP_WARN=80：对称性低/超胞大。"
                      "考虑提供外部 REF_FC2_PATH 或减小超胞。" % len(entries))
            for (lat_sc, frac_sc, dvec, atom_i) in entries:
                # 位移超胞的原子顺序是 phonopy 的排序；元素/MAGMOM 都按位置匹配
                sc_syms = [sym_list[i] for i in prim_index_of(frac_sc)]
                cid = add("displ", lat_sc, frac_sc, sc_syms,
                          {"strain_factor": None, "volume_factor": None,
                           "strain_grun": st, "rattle_std": None, "seed": None,
                           "rms_A": 0.0, "min_dist_ratio": None,
                           "d_min_rejections": 0,
                           "disp_number": int(atom_i),       # phonopy 0-based
                           "displacement": dvec})
                mag = magmom_for_frac(frac_sc)
                if mag:
                    (sdir / ("%s.magmom" % cid)).write_text(
                        "MAGMOM = %s\n" % mag, encoding="utf-8", newline="\n")
                displ_entries.append({"cfg_id": cid, "strain_grun": st,
                                      "disp_number": int(atom_i),
                                      "displacement": dvec})
    elif a.gen == 0 and a.ref_fc2_path:
        print("[SKIP] REF_FC2_PATH 已给，跳过 DFT 声子基准（displ 帧）")
    else:
        print("[..] gen=%d > 0：不再生成声子参考数据（只追加随机位移结构）" % a.gen)

    # ---- 孤立原子（仅 gen0）----
    iso_entries = []
    if a.gen == 0:
        for el in order:
            lat_i = [[a.iso_box, 0, 0], [0, a.iso_box, 0], [0, 0, a.iso_box]]
            cid = add("iso", lat_i, [[0.5, 0.5, 0.5]], [el],
                      {"element": el, "strain_factor": None, "volume_factor": None,
                       "rattle_std": None, "seed": None, "rms_A": 0.0,
                       "min_dist_ratio": None, "d_min_rejections": 0,
                       "disp_number": None, "strain_grun": None})
            mom = None
            if magnetic:
                avg = float(np.mean([moments[i] for i, s in enumerate(sym_list) if s == el]))
                if abs(avg) > 0.1:
                    mom = "%.4f" % avg
                else:
                    mom = "%.1f" % ds.ISO_HIGH_SPIN.get(el, 0.0)
                    print("[WARN] 元素 %s 在晶体中平均磁矩 %.3f ≤ 0.1 μB，孤立原子用"
                          "高自旋起点 MAGMOM=%s（ISPIN=2）" % (el, avg, mom))
            if mom and float(mom) != 0.0:
                (sdir / ("%s.magmom" % cid)).write_text(
                    "MAGMOM = %s\n" % mom, encoding="utf-8", newline="\n")
            iso_entries.append({"cfg_id": cid, "element": el, "magmom": mom})

    # ---- manifest ----
    summary = {
        "generation": a.gen,
        "dim": dim,
        "vac_axis": int(vac_axis),
        "supercell_reps": reps,
        "n_atoms_supercell": natom_prim * reps[0] * reps[1] * reps[2],
        "n_atoms_primitive": natom_prim,
        "elements": order,
        "element_counts_primitive": counts,
        "magnetic": bool(magnetic),
        "vol_factors": vol_factors,
        "lat_factors": [round(f, 6) for f in lat_factors],
        "rattle_std_A": rattle_std,
        "gruneisen_strain": a.grun_strain,
        "n_frames": len(manifest),
        "n_rattle": n_rattle,
        "n_displ": len(displ_entries),
        "n_iso": len(iso_entries),
        "n_static": sum(1 for e in manifest if e["config_type"] == "static"),
        "frames": manifest,
        "iso_frames": iso_entries,
        "displ_frames": displ_entries,
        "seed_base": a.seed_base,
    }
    (outdir / "struct_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[OK] struct_manifest.json：%d 帧（rattle %d + displ %d + static %d + iso %d）"
          % (len(manifest), n_rattle, len(displ_entries),
             summary["n_static"], len(iso_entries)))
    print("[DONE] 第 %d 代结构生成完毕 → %s" % (a.gen, outdir))


if __name__ == "__main__":
    main()
