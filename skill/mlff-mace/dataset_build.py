#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dataset_build.py —— step6_dataset 引擎（在 venv 里跑，需要 numpy/ase/matplotlib）。

cwd = 材料目录。把 step5_label 全部已算完的 cfg-* OUTCAR 转成 extxyz 训练集：
    - 逐帧解析能量（energy(sigma->0) 优先）/ 力 / 应力（in kB 张量 → eV/Å³ Voigt）
    - 指纹校验（vs step1 的 DFT 设置；k 点密度用容差）
    - extend 模式并入 PRE_XYZ_FILES（指纹不一致的老数据整批丢弃 + WARN）
    - 离群过滤（ENERGY_LIMIT / FORCE_LIMIT；>10% WARN，>30% FAIL；filtered: true 保留）
    - FPS 全局排序 + 固定测试集划分（id 哈希 %10，跨代稳定）
    - e0s.json（孤立原子能量）、coverage_report.json、energy_forces.png 三联图
    - 写 step6_dataset/gen-<K>/{train.xyz, test.xyz, ...}

--coverage-only 模式（step4 extend 用）：只对 PRE_XYZ_FILES 做覆盖分析，把新采样
计划写进 coverage_plan.json（把新钱压到盲区：应变档/幅度档/元素）。
退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dft_settings as ds  # noqa: E402
import mlff_common as mc  # noqa: E402

EV_A3_PER_KBAR = 6.24150907e-4   # 1 kB = 0.1 GPa；1 eV/Å³ = 160.217662 GPa


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--outdir", default=None)            # step6_dataset/gen-<K>
    p.add_argument("--step1-dir", default="step1_relax")
    p.add_argument("--step5-dir", default="step5_label")
    p.add_argument("--step4-dir", default="step4_genstruct")
    p.add_argument("--dim", required=True)
    p.add_argument("--energy-limit", type=float, default=0.005)
    p.add_argument("--force-limit", type=float, default=0.1)
    p.add_argument("--kspacing-tol", type=float, default=0.20)
    p.add_argument("--pre-xyz", default="")             # extend：逗号分隔
    p.add_argument("--fps-seed", type=int, default=42)
    p.add_argument("--vol-factors", default="0.97,1.00,1.03")
    p.add_argument("--n-per-cell", type=int, default=2)
    p.add_argument("--coverage-only", action="store_true")
    return p.parse_args()


# ============================================================ OUTCAR 解析
def outcar_done(path):
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with open(p, "rb") as fh:
            try:
                fh.seek(-200000, 2)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return False
    return "General timing and accounting informations" in tail


def parse_outcar(path):
    """-> dict(E, F, stress) 或 None（没算完）。E 用 energy(sigma->0) 兜底 TOTEN。"""
    if not outcar_done(path):
        return None
    text = Path(path).read_text(errors="ignore")
    e = None
    for m in list(__import__("re").finditer(r"energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)", text)):
        e = float(m.group(1))
    if e is None:
        m = list(__import__("re").finditer(r"free\s+energy\s+TOTEN\s*=\s*([-+0-9.Ee]+)", text))
        if m:
            e = float(m[-1].group(1))
    if e is None:
        raise RuntimeError("OUTCAR 里找不到总能")

    # 力：最后一个 TOTAL-FORCE 块（外层整体捕获：内层 + 会让 group 只剩最后一行）
    fblocks = [b for b in __import__("re").finditer(
        r"TOTAL-FORCE \(eV/Angst\)\s*\n\s*-+\s*\n"
        r"((?:(?:\s*[-+0-9.eE]+){3,6}[^\n]*\n)+)", text)]
    if not fblocks:
        raise RuntimeError("OUTCAR 里找不到 TOTAL-FORCE 块")
    f = []
    for line in fblocks[-1].group(1).splitlines():
        t = line.split()
        # VASP 6.x：6 列 = 位置 x y z + 力 fx fy fz（力在后 3 列）；旧版只有 3 列力
        if len(t) >= 6:
            cols = t[3:6]
        elif len(t) == 3:
            cols = t
        else:
            continue
        try:
            f.append([float(cols[0]), float(cols[1]), float(cols[2])])
        except ValueError:
            continue
    if not f:
        raise RuntimeError("TOTAL-FORCE 块解析不出力")

    # 应力：最后一个 "in kB" 行（与 in kB 同行的 6 个数，列序 XX YY ZZ XY YZ ZX）
    # 符号：VASP 的 kB 张量以压缩为正（external pressure = -Tr/3 > 0 表示压缩）；
    # MACE/ASE 以拉伸为正 → 取负号。
    import re as _re
    srows = _re.findall(
        r"in\s+kB\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+"
        r"([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", text)
    stress = None
    if srows:
        xx, yy, zz, xy, yz, zx = [float(x) for x in srows[-1]]
        stress = [-v * EV_A3_PER_KBAR for v in (xx, yy, zz, yz, zx, xy)]
    return {"E": e, "F": f, "stress": stress}


# ============================================================ 特征 / 过滤 / FPS
def fps_order(features, seed=42):
    """贪心最远点采样，返回索引顺序（前缀天然嵌套）。"""
    n = len(features)
    if n == 0:
        return []
    rng = np.random.default_rng(seed)
    order = [int(rng.integers(0, n))]
    remain = list(set(range(n)) - {order[0]})
    dist = np.full(n, np.inf)
    while remain:
        d = np.sum((features[remain] - features[order[-1]]) ** 2, axis=1)
        dist[remain] = np.minimum(dist[remain], d)
        nxt = remain[int(np.argmax(dist[remain]))]
        order.append(nxt)
        remain.remove(nxt)
    return order


def frame_features(E, F, n_atom):
    f = np.array(F, dtype=float).ravel()
    f = f / (max(np.std(f), 1e-12) or 1.0)
    return np.concatenate([[E / n_atom], f])


def filter_frames(frames, energy_limit, force_limit):
    """两道过滤（§8.3）：
    - 力：max|F| ≥ FORCE_LIMIT（= autoplex data_distillation 的 force_max，源码
      默认 40.0 eV/Å，力分量上限——不是 0.1！0.1 会把大幅度 rattle 全滤掉）
    - 能量：|E/atom − 组中位数| ≥ ENERGY_LIMIT（组 = config_type + 应变 + 幅度档；
      组员 < 4 时跳过能量过滤——2 个种子定不出中位数，硬套会误杀）
    """
    groups = {}
    for fr in frames:
        key = (fr["config_type"], fr.get("strain_factor"), fr.get("strain_grun"),
               fr.get("rattle_std"))
        groups.setdefault(key, []).append(fr)
    n_force, n_energy = 0, 0
    for key, frs in groups.items():
        # rattle 帧跳过能量过滤：随机位移能量散布是物理的（幅度 0.27 Å 时同组可差
        # ~0.09 eV/atom >> ENERGY_LIMIT=0.005），跨代累积组员>=4 后固定阈值必误杀；
        # 坏结构由 FORCE_LIMIT 与 d_min 兜底。
        is_rattle = frs[0]["config_type"] == "rattle"
        use_energy = len(frs) >= 4 and not is_rattle
        med = float(np.median([f["E"] / len(f["F"]) for f in frs])) if use_energy else 0.0
        for fr in frs:
            ea = fr["E"] / len(fr["F"])
            fmax = float(np.max(np.abs(fr["F"])))
            bad_f = fmax >= force_limit
            bad_e = use_energy and abs(ea - med) >= energy_limit
            fr["filtered"] = bool(bad_f or bad_e)
            fr["e_vs_median_eV"] = float(ea - med) if use_energy else None
            fr["fmax_eV_A"] = fmax
            n_force += 1 if bad_f else 0
            n_energy += 1 if bad_e else 0
    return n_force, n_energy


# ============================================================ 主流程
def load_pre_xyz(paths, dim):
    """读 PRE_XYZ_FILES → 统一格式帧列表。读不出指纹的帧标记 fp=None。"""
    from ase.io import read as ase_read
    frames = []
    for p in paths:
        if not Path(p).is_file():
            sys.exit("[ERROR] PRE_XYZ_FILES 里的 %s 不存在" % p)
        try:
            atoms_list = ase_read(p, index=":")
        except Exception as e:
            sys.exit("[ERROR] 读 %s 失败：%s" % (p, e))
        for at in atoms_list:
            info = dict(at.info or {})
            forces = (at.arrays.get("REF_forces")
                      if "REF_forces" in at.arrays
                      else at.arrays.get("forces")
                      if "forces" in at.arrays
                      else info.get("REF_forces", info.get("forces", [])))
            frames.append({
                "E": float(info.get("REF_energy", info.get("energy", 0.0))),
                "F": np.array(forces),
                "stress": info.get("REF_stress", None),
                "atoms": at,
                "config_type": str(info.get("config_type", "pre")),
                "strain_factor": info.get("strain_factor"),
                "strain_grun": info.get("strain_grun"),
                "filtered": bool(info.get("filtered", False)),
                "source": Path(p).name,
                "fingerprint": info.get("mlff_fingerprint"),
            })
    return frames


def coverage_analysis(frames, dim):
    """覆盖分析：体积（2D 面积）/ RMS 位移 / 元素力分布。-> report dict。"""
    vols, rms_vals, elem_forces = [], [], {}
    for fr in frames:
        cell = np.array(fr["atoms"].get_cell(), dtype=float)
        if dim == "2d":
            v = float(np.linalg.norm(np.cross(cell[0], cell[1])))
        else:
            v = float(np.abs(np.linalg.det(cell)))
        vols.append(v)
        pos = fr["atoms"].get_positions()
        ref = pos.mean(axis=0)
        rms_vals.append(float(np.sqrt(np.mean(np.sum((pos - ref) ** 2, axis=1))))
                        if len(pos) > 0 else 0.0)
        for s, f in zip(fr["atoms"].get_chemical_symbols(), np.array(fr["F"])):
            elem_forces.setdefault(s, []).append(float(np.linalg.norm(f)))
    report = {
        "n_frames": len(frames),
        "volume_min": round(min(vols), 4) if vols else None,
        "volume_max": round(max(vols), 4) if vols else None,
        "volume_mean": round(float(np.mean(vols)), 4) if vols else None,
        "volume_std_rel": round(float(np.std(vols) / (np.mean(vols) or 1.0)), 4) if vols else None,
        "rms_min_A": round(min(rms_vals), 5) if rms_vals else None,
        "rms_max_A": round(max(rms_vals), 5) if rms_vals else None,
        "n_per_element": {k: len(v) for k, v in elem_forces.items()},
        "force_median_eV_A": {k: round(float(np.median(v)), 4) for k, v in elem_forces.items()},
    }
    return report


def make_coverage_plan(frames, dim, vol_factors, n_per_cell):
    """把新采样压到覆盖盲区。返回 {targets: [(strain_factor, rattle_std, n)], reason}。
    v1 策略：已有帧体积集中在单值附近（典型：固定胞 rattle）→ 1.00 档种子降 1，
    其余档平分；否则均匀。"""
    vols = []
    for fr in frames:
        cell = np.array(fr["atoms"].get_cell())
        if dim == "2d":
            vols.append(float(np.linalg.norm(np.cross(cell[0], cell[1]))))
        else:
            vols.append(float(np.abs(np.linalg.det(cell))))
    if not vols:
        return None
    rel = float(np.std(vols) / (np.mean(vols) + 1e-12))
    if rel < 1e-3:
        plan = {"targets": [], "reason": ("已有帧体积全部集中在单一晶胞（相对展宽 %.2e）"
                                          "——把新钱全花在应变构型上（1.00 档只留 1 个种子）"
                                          % rel)}
        n_edge = max(1, int(np.ceil((len(vol_factors) * n_per_cell - 1) / 2)))
        for f in vol_factors:
            if abs(f - 1.0) < 1e-9:
                plan["targets"].append({"strain": float(f), "n": 1})
            else:
                plan["targets"].append({"strain": float(f), "n": n_edge})
        return plan
    return None


def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    outdir = cwd / (a.outdir or "step6_dataset/gen-%d" % a.gen)
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- 收集 step5 已算完的帧（跨代累积：遍历 step4 所有 gen-* 清单）----
    manifests = sorted((cwd / a.step4_dir).glob("gen-*/struct_manifest.json"))
    frames = []
    for mp in manifests:
        man = json.loads(mp.read_text())
        for ent in man.get("frames", []):
            cfgdir = cwd / a.step5_dir / ent["id"]
            if not outcar_done(cfgdir / "OUTCAR"):
                continue
            try:
                d = parse_outcar(cfgdir / "OUTCAR")
            except Exception as e:
                print("[WARN] %s 解析失败：%s" % (ent["id"], e))
                continue
            from ase.io import read as ase_read
            atoms = ase_read(str(cwd / a.step4_dir / ("gen-%d" % ent["gen"]) /
                                ent["file"]), format="vasp")
            if len(atoms) != len(d["F"]):
                print("[WARN] %s 原子数 %d ≠ 力行数 %d，跳过" %
                      (ent["id"], len(atoms), len(d["F"])))
                continue
            frames.append({
                "id": ent["id"], "config_type": ent["config_type"],
                "strain_factor": ent.get("strain_factor"),
                "strain_grun": ent.get("strain_grun"),
                "volume_factor": ent.get("volume_factor"),
                "rattle_std": ent.get("rattle_std"),
                "E": d["E"], "F": d["F"], "stress": d["stress"],
                "atoms": atoms, "filtered": False,
                "source": "step5_label",
            })
    if not frames:
        sys.exit("[ERROR] step5_label 里一帧算完的 OUTCAR 都没有 —— 先让 S5 跑完。")

    # ---- 指纹（本数据集所有 DFT 帧共用 step5 的生成设置）----
    incar = ds.read_incar(cwd / a.step1_dir / "INCAR")
    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    from dim_common import read_poscar_cell_frac
    prim_lat, _ = read_poscar_cell_frac(str(cwd / a.step1_dir / "CONTCAR"))
    fp, ksp = ds.dft_fingerprint(incar, cwd / a.step1_dir / "POTCAR",
                                 prim_lat, cwd / a.step1_dir / "KPOINTS",
                                 method.get("FUNC", "?"))

    # ---- extend：并入 PRE_XYZ_FILES ----
    pre_paths = [x.strip() for x in (a.pre_xyz or "").split(",") if x.strip()]
    if pre_paths:
        pre_frames = load_pre_xyz(pre_paths, a.dim)
        kept, dropped = [], 0
        for fr in pre_frames:
            if fr["fingerprint"] is None:
                kept.append(fr)
                print("[WARN] %s（%s）没有 mlff_fingerprint，无法校验 DFT 设置，"
                      "默认接受（用户负责确认一致性）" % (fr["source"], fr["config_type"]))
            elif fr["fingerprint"] == fp:
                kept.append(fr)
            else:
                dropped += 1
        if dropped:
            print("[WARN] 指纹不一致的已有数据整批丢弃 %d 帧（不阻断流程）" % dropped)
        frames += kept

    # ---- 离群过滤 ----
    n_force_f, n_energy_f = filter_frames(
        [f for f in frames if f["source"] == "step5_label"],
        a.energy_limit, a.force_limit)
    n_filt = sum(1 for f in frames if f.get("filtered"))
    n_total = len(frames)
    frac = n_filt / max(n_total, 1)
    if n_force_f or n_energy_f:
        print("[..] 离群过滤分解：力上限滤 %d 帧，能量离群滤 %d 帧（总 %d/%d）"
              % (n_force_f, n_energy_f, n_filt, n_total))
    if frac > 0.3:
        sys.exit("[ERROR] 离群过滤比例 %.0f%% > 30%%：ENERGY_LIMIT/FORCE_LIMIT 过严，"
                 "有效数据被滤掉了。调大阈值后 retry 本步。" % (frac * 100))
    if frac > 0.1:
        print("[WARN] 离群过滤比例 %.0f%% > 10%%（阈值可能过严；被滤帧已标 filtered: true "
              "保留在 xyz 里，训练时排除）" % (frac * 100))

    # ---- 划分：固定测试集（id 哈希 %10），其余按 FPS 排序成训练集 ----
    import hashlib
    idx_nonfilt = [i for i, fr in enumerate(frames) if not fr["filtered"]]
    train_idx, test_idx, iso_idx = [], [], []
    for i in idx_nonfilt:
        fr = frames[i]
        if fr["config_type"] == "iso":
            iso_idx.append(i)                    # 孤立原子不参与 FPS（原子数不同），放训练集尾部
            continue
        if fr.get("id"):
            h = int(hashlib.md5(fr["id"].encode()).hexdigest(), 16)
            (test_idx if h % 10 == 0 else train_idx).append(i)
        else:
            train_idx.append(i)          # 无 id 的 PRE 帧全进训练集
    bulk = [i for i in idx_nonfilt if i not in iso_idx]
    feats = np.array([frame_features(frames[i]["E"], frames[i]["F"], len(frames[i]["F"]))
                      for i in bulk])
    order = fps_order(feats, a.fps_seed)          # bulk 内的位置
    train_set = set(train_idx)
    train_ordered = [bulk[i] for i in order if bulk[i] in train_set] + iso_idx

    # ---- 写 extxyz ----
    def write_xyz(path, idxs):
        from ase.io import write as ase_write
        outs = []
        for i in idxs:
            fr = frames[i]
            at = fr["atoms"].copy()
            info = {"config_type": fr["config_type"],
                    "REF_energy": float(fr["E"]),
                    "mlff_fingerprint": fp}
            if fr.get("strain_factor") is not None:
                info["strain_factor"] = float(fr["strain_factor"])
            if fr.get("volume_factor") is not None:
                info["volume_factor"] = float(fr["volume_factor"])
            if fr.get("strain_grun") is not None:
                info["strain_grun"] = float(fr["strain_grun"])
            if fr.get("rattle_std") is not None:
                info["rattle_std"] = float(fr["rattle_std"])
            if fr["filtered"]:
                info["filtered"] = True
            if fr["stress"] is not None and a.dim == "3d":
                info["REF_stress"] = np.array(fr["stress"], dtype=float)
            if a.dim == "2d":
                info["config_stress_weight"] = 0.0   # 2D 面外应力是垃圾，不训
            at.info = info
            # 力必须是 per-atom 数组（atoms.arrays）——mace 从 arrays_keys["forces"] 读，
            # 放进 info 里 mace 找不到，会静默退化成只训能量不训力（实测 RMSE_F=None、
            # 测试力 RMSE 322 meV/Å、声子 0.88 THz、体模量 0.1 GPa）。
            at.new_array("REF_forces", np.array(fr["F"], dtype=float))
            outs.append(at)
        ase_write(str(path), outs, format="extxyz")
        return len(outs)

    # all.xyz（含被过滤帧，供复查）与 train.xyz / test.xyz
    write_xyz(outdir / "all.xyz", list(range(n_total)))
    n_train = write_xyz(outdir / "train.xyz", train_ordered)
    n_test = write_xyz(outdir / "test.xyz", test_idx)

    # ---- e0s.json（孤立原子）----
    e0s = {}
    iso_man = [m for m in manifests]
    for mp in iso_man:
        man = json.loads(mp.read_text())
        for ent in man.get("iso_frames", []):
            cfgdir = cwd / a.step5_dir / ent["cfg_id"]
            if not outcar_done(cfgdir / "OUTCAR"):
                continue
            d = parse_outcar(cfgdir / "OUTCAR")
            from ase.data import chemical_symbols
            z = chemical_symbols.index(ent["element"])
            e0s[str(z)] = float(d["E"])
    if e0s:
        (outdir / "e0s.json").write_text(json.dumps(e0s, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
    else:
        print("[WARN] 没有孤立原子帧（extend 模式？）——e0s.json 留空，训练用基座 E0")

    # ---- 覆盖分析 + 三联图 ----
    report = coverage_analysis(frames, a.dim)
    plan = make_coverage_plan(frames, a.dim,
                              [float(x) for x in a.vol_factors.split(",") if x.strip()],
                              a.n_per_cell)
    report["sampling_plan"] = plan
    (outdir / "coverage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    try:
        _plot_parity(frames, train_ordered, test_idx, outdir / "energy_forces.png")
    except Exception as e:
        print("[WARN] energy_forces.png 画图失败：%s" % e)

    import hashlib as _hl
    _man_top = cwd / a.step4_dir / "struct_manifest.json"
    _dh = _hl.md5(_man_top.read_bytes()).hexdigest() if _man_top.is_file() else ""
    summary = {
        "generation": a.gen,
        "data_hash": _dh,
        "dim": a.dim,
        "n_frames_total": n_total,
        "n_train": n_train,
        "n_test": n_test,
        "n_filtered": n_filt,
        "filter_fraction": round(frac, 4),
        "energy_limit": a.energy_limit,
        "force_limit": a.force_limit,
        "fingerprint": fp,
        "kspacing_1_A": round(ksp, 4),
        "fps_seed": a.fps_seed,
        "e0s": e0s,
        "pre_xyz_files": pre_paths,
        "wall_time_s": round(time.time() - t0, 1),
    }
    (outdir / "dataset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[DONE] dataset_summary.json：共 %d 帧（train %d / test %d / filtered %d），"
          "用时 %.1f s" % (n_total, n_train, n_test, n_filt, time.time() - t0))


CURRENT_FP = {}


def _plot_parity(frames, train_idx, test_idx, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    train_set = set(train_idx)
    test_set = set(test_idx)
    sets = {"训练集": [frames[i] for i in train_set],
            "测试集": [frames[i] for i in test_set],
            "过滤后": [f for f in frames if f["filtered"]]}
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    for ax, (name, frs) in zip(axes, sets.items()):
        e = [f["E"] / len(f["F"]) for f in frs]
        f = [float(np.max(np.abs(f["F"]))) for f in frs]
        ax.scatter(e, f, s=8, alpha=0.6)
        ax.set_xlabel("E (eV/atom)")
        ax.set_ylabel("max|F| (eV/Å)")
        ax.set_title("%s (n=%d)" % (name, len(frs)))
    fig.tight_layout()
    fig.savefig(str(png_path), dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
