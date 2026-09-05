#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""learning_curve.py —— 学习曲线引擎（在 venv 里跑）。不花任何 DFT。

对全部已标注帧按 FPS 顺序取前缀子集（S25 ⊂ S50 ⊂ S100 ⊂ …，前缀天然嵌套，
不需要重采样），每个点**独立从基座模型开始**微调（同一 seed、同一套超参，
不许 warm-start 上一个点），记两条曲线：test 力 RMSE (meV/Å) + 声子频率 MAE (THz)。
判平：从 N 到 2N，两条指标相对改善均 < CURVE_TOL → curve_plateau = true。

cwd = 材料目录。输出 step8_benchmark/gen-<K>/curve/{learning_curve.json, learning_curve.png}。
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
import benchmark as bm  # noqa: E402（复用 phonopy_from_model / freqs_on_mesh / 弛豫）
import mace_model as mm  # noqa: E402
import mlff_common as mc  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--mat", required=True)
    p.add_argument("--outdir", required=True)          # step8_benchmark/gen-<K>/curve
    p.add_argument("--train", required=True)
    p.add_argument("--test", required=True)
    p.add_argument("--foundation", required=True)
    p.add_argument("--replay", default="")
    p.add_argument("--e0s", default="")
    p.add_argument("--device", default="cpu")
    p.add_argument("--points", default="25,50,100,200,all")
    p.add_argument("--tol", type=float, default=0.05)
    p.add_argument("--epochs", type=int, default=1500)
    p.add_argument("--valid-fraction", type=float, default=0.10)
    # [FIX] 小数据集曲线点要更大验证份额：帧数 < 40 时验证集太小（10% 只有 2-3 帧）
    # 会让 mace 训不动/早停噪声大（GaAs n=25 实测失败）。调用方（benchmark）按
    # n_train 传合理的 --valid-fraction（<40 帧用 0.2）。
    p.add_argument("--patience", type=int, default=100)
    p.add_argument("--start-swa", type=int, default=1200)
    p.add_argument("--loss", default="huber")
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--energy-weight", type=float, required=True)
    p.add_argument("--forces-weight", type=float, required=True)
    p.add_argument("--stress-weight", type=float, required=True)
    p.add_argument("--ref-freqs", default="")          # 存 DFT 基准频率（幂等续跑）
    p.add_argument("--ref-disp", type=float, default=0.1)   # 与 step4 生成 displ 帧用的 REF_DISP 一致
    p.add_argument("--dim", required=True)
    p.add_argument("--matdir", default="")
    p.add_argument("--sc-summary", default="step2_supercell/supercell_summary.json")
    p.add_argument("--model", required=True)           # seed-1 模型（曲线里 all 点可复用）
    return p.parse_args()


def finetune(name, train_prefix, a, cwd, seed):
    """独立微调（同一 seed/超参），返回模型路径。"""
    from ase.io import read as ase_read
    wd = cwd / ("pt-%s" % name)
    wd.mkdir(parents=True, exist_ok=True)
    atoms = ase_read(a.train, index=":")
    from ase.io import write as ase_write
    ase_write(str(wd / "train_prefix.xyz"), atoms[:train_prefix], format="extxyz")
    cmd = [sys.executable, "-m", "mace.cli.run_train",
           "--name=curve_%s" % name,
           "--foundation_model=%s" % a.foundation,
           "--multiheads_finetuning=True",
           "--pt_train_file=%s" % a.replay,
           "--num_samples_pt=30000",
           "--train_file=%s" % (wd / "train_prefix.xyz"),
           "--valid_fraction=%g" % a.valid_fraction,
           "--test_file=%s" % a.test,
           "--energy_key=REF_energy", "--forces_key=REF_forces",
           "--stress_key=REF_stress",
           "--energy_weight=%g" % a.energy_weight,
           "--forces_weight=%g" % a.forces_weight,
           "--stress_weight=%g" % a.stress_weight,
           "--lr=%g" % a.lr, "--max_num_epochs=%d" % a.epochs,
           "--batch_size=%d" % a.batch_size,
           "--patience=%d" % a.patience, "--swa", "--start_swa=%d" % a.start_swa,
           "--ema", "--ema_decay=0.99", "--amsgrad",
           "--scaling=rms_forces_scaling",
           "--default_dtype=float64", "--device=%s" % a.device,
           "--seed=%d" % seed, "--loss=%s" % a.loss, "--eval_interval=2",
           "--force_mh_ft_lr=true"]
    if a.e0s and Path(a.e0s).is_file():
        cmd.append("--E0s=%s" % a.e0s)
    else:
        cmd.append("--E0s=foundation")
    log = wd / "train.log"
    with open(str(log), "w") as fh:
        subprocess.run(cmd, cwd=str(wd), stdout=fh, stderr=subprocess.STDOUT)
    cand = sorted(wd.rglob("*.model"), key=lambda c: c.stat().st_mtime)
    if not cand:
        print("[FAIL] 曲线点 %s 没训出模型，看 %s" % (name, log))
        return None
    return str(cand[-1])


def eval_point(model_path, a, cwd, matdir, ref_freqs, reps):
    """-> (test 力 RMSE meV/Å, 声子 MAE THz)。声子用模型自弛豫 + 同一超胞网格。"""
    from ase.io import read as ase_read
    test_atoms = ase_read(a.test, index=":")
    calc, _ = mm.build_calculator(model_path, [cwd], a.device, "float64")
    f_ref = np.array([at.arrays.get("REF_forces", at.info.get("REF_forces"))
                      for at in test_atoms])
    f_pred = np.array([bm.eval_model(calc, at)[1] for at in test_atoms])
    f_rmse = float(np.sqrt(np.mean((f_pred - f_ref) ** 2))) * 1000.0

    # 声子：自弛豫 + fc2 + 网格频率 vs DFT 基准
    wd = Path(model_path).parent
    rel = wd / "relax"
    rel.mkdir(exist_ok=True)
    (rel / "POSCAR").write_text((matdir / "step1_relax" / "CONTCAR").read_text(),
                                encoding="utf-8")
    subprocess.run([sys.executable,
                    str(Path(__file__).resolve().parent / "mace_relax.py"),
                    "--model", model_path, "--device", a.device,
                    "--dtype", "float64", "--dim", a.dim,
                    "--relax", "true", "--relax-cell", "true",
                    "--fmax", "1e-4", "--steps", "2000", "--opt", "FIRE",
                    "--fix-symmetry", "true", "--cell-policy", "none",
                    "--residual-tol", "1e-3"],
                   cwd=str(rel), capture_output=True)
    if not (rel / "CONTCAR").is_file():
        return f_rmse, None
    if ref_freqs is None:          # [FIX-lite] 无 DFT 基准（displ 未算/无 REF_FC2）→ 只出力曲线
        return f_rmse, None
    prim_m = ase_read(str(rel / "CONTCAR"), format="vasp")
    ph_m, _, _ = bm.phonopy_from_model(prim_m, reps, calc, 0.01)
    f_m, _, _ = bm.freqs_on_mesh(ph_m, reps, with_eig=False)
    mae = float(np.mean(np.abs(bm.sorted_freqs(f_m) - bm.sorted_freqs(ref_freqs))))
    return f_rmse, mae


def main():
    a = parse_args()
    t0 = time.time()
    cwd = Path.cwd()
    # 材料目录 = outdir(step8_benchmark/gen-K/curve) 往上三级
    matdir = Path(a.matdir) if a.matdir else (Path(a.outdir).parent.parent.parent
                                            if Path(a.outdir).is_absolute()
                                            else cwd / Path(a.outdir).parent.parent.parent)
    if not Path(a.sc_summary).is_absolute():
        a.sc_summary = str(matdir / a.sc_summary)
    outdir = cwd / a.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    from ase.io import read as ase_read
    n_train = len(ase_read(a.train, index=":"))
    want = []
    for tok in a.points.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "all":
            want.append(n_train)
        else:
            n = int(tok)
            if n < n_train:
                want.append(n)
    if n_train not in want:
        want.append(n_train)
    want = sorted(set(want))
    print("[..] 曲线点：%s（n_train=%d）" % (want, n_train))

    sc_sum = json.loads((cwd / a.sc_summary).read_text())
    reps = [int(x) for x in sc_sum["supercell_reps"]]

    # DFT 基准频率（幂等：ref_freqs.npy 存在就复用，免重算）
    # [FIX-lite] 无 displ 帧力 → ref_freqs=None，声子 MAE 曲线 NA（只出力 RMSE 曲线），不崩
    ref_path = Path(a.ref_freqs) if a.ref_freqs else outdir / "ref_freqs.npy"
    ref_freqs = None
    if ref_path.is_file():
        ref_freqs = np.load(str(ref_path))
        print("[..] 复用 DFT 基准频率 %s" % ref_path)
    else:
        prim_dft = ase_read(str(matdir / "step1_relax" / "CONTCAR"), format="vasp")
        man = mc.read_json(matdir / "step4_genstruct" / "struct_manifest.json", {})
        # displ 帧只在第 0 代生成，后续代从 gen-0 清单复用（同 benchmark.py）
        if int(man.get("generation", 0)) > 0:
            _m0 = mc.read_json(matdir / "step4_genstruct" / "gen-0" / "struct_manifest.json", {})
            man["displ_frames"] = _m0.get("displ_frames", [])
        from dataset_build import parse_outcar, outcar_done
        d0 = [e for e in man.get("displ_frames", []) if abs(e["strain_grun"]) < 1e-9]
        _miss = [e["cfg_id"] for e in d0
                 if not outcar_done(matdir / "step5_label" / e["cfg_id"] / "OUTCAR")]
        if d0 and not _miss:
            forces = [parse_outcar(matdir / "step5_label" / e["cfg_id"] / "OUTCAR")["F"]
                      for e in d0]
            ph_dft = bm.phonopy_from_manifest(prim_dft, reps, d0, forces, a.ref_disp)
            ref_freqs, _, _ = bm.freqs_on_mesh(ph_dft, reps, with_eig=False)
            ref_freqs = bm.sorted_freqs(ref_freqs)
            np.save(str(ref_path), ref_freqs)
        else:
            print("[WARN] 轻量模式：无可用 displ 帧力（缺 %d/%d）→ 学习曲线只出力 RMSE"
                  % (len(_miss), len(d0)))

    rows = []
    for n in want:
        tag = "all" if n == n_train else str(n)
        model = a.model if n == n_train else finetune(tag, n, a, outdir, 1)
        if model is None:
            rows.append({"n": n, "force_rmse_meV_A": None, "phonon_mae_THz": None})
            continue
        f_rmse, mae = eval_point(model, a, cwd, matdir, ref_freqs, reps)
        rows.append({"n": int(n), "force_rmse_meV_A": round(f_rmse, 2),
                     "phonon_mae_THz": (round(mae, 4) if mae is not None else None)})
        print("[..] n=%d：test 力 RMSE %.2f meV/Å，声子 MAE %s THz"
              % (n, f_rmse, ("%.3f" % mae) if mae is not None else "NA"), flush=True)

    # ---- 判平：从 N 到 2N，两条指标相对改善均 < tol ----
    plateau = False
    for i in range(len(rows) - 1):
        if rows[i]["n"] * 2 != rows[i + 1]["n"]:
            continue
        r0, r1 = rows[i], rows[i + 1]
        rel_f = (r0["force_rmse_meV_A"] - r1["force_rmse_meV_A"]) / max(r0["force_rmse_meV_A"], 1e-9)
        if None in (r0["phonon_mae_THz"], r1["phonon_mae_THz"]):
            # [FIX-lite] 无 DFT 基准（声子 MAE NA）→ 只用力 RMSE 判平
            if rel_f < a.tol:
                plateau = True
                break
            continue
        rel_p = (r0["phonon_mae_THz"] - r1["phonon_mae_THz"]) / max(r0["phonon_mae_THz"], 1e-9)
        if rel_f < a.tol and rel_p < a.tol:
            plateau = True
            break

    result = {"gen": a.gen, "curve_tol": a.tol, "points": rows,
              "curve_plateau": bool(plateau),
              "note": ("两条指标从 N 到 2N 的相对改善均 < %.2f 判平；"
                       "它们经常不同步收敛，声子 MAE 才是本技能关心的量" % a.tol),
              "wall_time_s": round(time.time() - t0, 1)}
    (outdir / "learning_curve.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    try:
        _plot(rows, outdir / "learning_curve.png")
    except Exception as e:
        print("[WARN] learning_curve.png 画图失败：%s" % e)
    print("[DONE] learning_curve.json：plateau=%s，%.1f min"
          % (plateau, (time.time() - t0) / 60.0))


def _plot(rows, png_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ns = [r["n"] for r in rows]
    f = [r["force_rmse_meV_A"] for r in rows]
    p = [r["phonon_mae_THz"] for r in rows]
    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    ax1.plot(ns, f, "o-", color="tab:blue", label="test 力 RMSE (meV/Å)")
    ax1.set_xlabel("训练帧数")
    ax1.set_ylabel("力 RMSE (meV/Å)", color="tab:blue")
    ax2 = ax1.twinx()
    ax2.plot(ns, p, "s--", color="tab:red", label="声子频率 MAE (THz)")
    ax2.set_ylabel("声子 MAE (THz)", color="tab:red")
    ax1.set_title("学习曲线（FPS 嵌套前缀，各点独立从基座微调）")
    fig.tight_layout()
    fig.savefig(str(png_path), dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    main()
