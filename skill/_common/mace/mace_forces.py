#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_forces.py —— 位移超胞的 MACE 取力（step2_disp_force 的实际干活脚本）。

**在 conda 环境里跑，一般由 submit.sh 在计算节点执行**，cwd = step2_disp_force/。
读 phono3py_disp.yaml → 逐个位移超胞算力 → 写 FORCES_FC3 [+ FORCES_FC2]
+ phono3py_params.yaml + forces_summary.json。

和 kl-dft-cpu（VASP）路线的关键差别：**不扇出**。VASP 那边每个位移一个 sbatch，因为一个
位移就要几十核时；MACE 一个位移是秒级，几百个位移在一个作业里串完，省掉几百次排队。

两件容易被忽略但会毁掉结果的事，本脚本都处理了：
  1. **残余力扣除**：先算未位移超胞的力 F0，从每个位移超胞的力里减掉。理想情况
     F0≈0，扣不扣无所谓；F0 不小则说明 step1 的弛豫没到位，此时不扣会直接违反
     声学求和规则（ASR），Γ 点声学支跑出非零频率。同时 F0 本身就是最好的体检指标。
  2. **断点续算**：每 CKPT 个结构把力落盘。作业被墙钟砍掉后 tf retry 从断点接着跑，
     不用从头再来。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mace_model as mm


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--disp-yaml", default="phono3py_disp.yaml")
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--device", default="auto")
    p.add_argument("--dtype", default="float64")
    p.add_argument("--ckpt", type=int, default=20)
    p.add_argument("--subtract-residual", default="true")
    p.add_argument("--residual-warn", type=float, default=5e-3)
    return p.parse_args()


def as_bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on", ".true.")


def pa_positions(pa):
    pos = getattr(pa, "positions", None)
    return np.array(pos if pos is not None else pa.get_positions(), dtype=float)


def compute_set(calc, base_atoms, scells, tag, ckpt_every, f0):
    """对一组位移超胞逐个取力，带断点续算。-> (N, natom, 3)。"""
    ck = Path("forces_%s.ckpt.npy" % tag)
    done = []
    if ck.is_file():
        try:
            done = list(np.load(str(ck)))
            print("[..] %s 断点续算：已有 %d/%d 帧" % (tag, len(done), len(scells)))
        except Exception as e:
            print("[WARN] 断点文件损坏（%s），从头算" % e)
            done = []

    t0, n = time.time(), len(scells)
    for i in range(len(done), n):
        sc = scells[i]
        if sc is None:                       # --cutoff-pair 跳过的对，力按 0 记
            done.append(np.zeros_like(f0))
        else:
            base_atoms.set_positions(pa_positions(sc))
            f = np.array(base_atoms.get_forces(), dtype=float) - f0
            done.append(f)
        if (i + 1) % ckpt_every == 0 or i + 1 == n:
            np.save(str(ck), np.array(done))
            el = time.time() - t0
            rate = (i + 1 - 0) / max(el, 1e-9)
            eta = (n - i - 1) / max(rate, 1e-9)
            print("[..] %s %d/%d  %.2f 帧/s  ETA %.1f min"
                  % (tag, i + 1, n, rate, eta / 60.0), flush=True)
    return np.array(done)


def write_forces_file(dataset, forces, filename, order):
    """优先用 phono3py.file_IO 的官方写出口；不同版本签名变过，兜底手写。"""
    try:
        from phono3py.file_IO import write_FORCES_FC3, write_FORCES_FC2
        w = write_FORCES_FC3 if order == 3 else write_FORCES_FC2
        w(dataset, forces, filename=filename)
        return "file_IO"
    except Exception as e:
        print("[WARN] file_IO 写 %s 失败（%s），改用手写格式" % (filename, e))
        with open(filename, "w") as fh:
            for i, fset in enumerate(forces):
                fh.write("# %d\n" % (i + 1))
                for v in fset:
                    fh.write("%15.10f %15.10f %15.10f\n" % (v[0], v[1], v[2]))
        return "manual"


def main():
    a = parse_args()
    import phono3py

    if not Path(a.disp_yaml).is_file():
        sys.exit("[ERROR] 找不到 %s（step2 的 gen 没跑成？）" % a.disp_yaml)
    ph3 = phono3py.load(a.disp_yaml, produce_fc=False, log_level=1)

    scells = ph3.supercells_with_displacements
    if not scells:
        sys.exit("[ERROR] phono3py 里没有位移超胞")
    perfect = ph3.supercell
    natom = len(pa_positions(perfect))
    print("[..] fc3 超胞 %d 原子 × %d 个位移" % (natom, len(scells)))

    cwd = Path.cwd()
    calc, desc = mm.build_calculator(a.model, [cwd, cwd.parent, a.model_dir],
                                     a.device, a.dtype)
    dev = mm.pick_device(a.device)
    if dev == "cpu":
        # torch 默认可能只用一半核，或者反过来在共享节点上开满抢别人的。
        # 以 SLURM 分给本作业的核数为准。
        try:
            import torch
            nth = int(os.environ.get("SLURM_CPUS_PER_TASK")
                      or os.environ.get("OMP_NUM_THREADS") or 0)
            if nth > 0:
                torch.set_num_threads(nth)
                print("[..] torch CPU 线程数 = %d" % nth)
        except Exception as e:
            print("[WARN] 设置 torch 线程数失败：%s" % e)
    print("[OK] MACE calculator：%s" % desc)

    base = mm.phonopy_atoms_to_ase(perfect)
    base.calc = calc
    t0 = time.time()
    f0_raw = np.array(base.get_forces(), dtype=float)
    f0max = float(np.max(np.linalg.norm(f0_raw, axis=1)))
    print("[..] 未位移超胞残余力 max=%.3e eV/Å（单帧 %.2f s）"
          % (f0max, time.time() - t0))
    if f0max > a.residual_warn:
        print("[WARN] 残余力偏大。说明 step1 弛豫的结构和本势不完全自洽（换了模型？"
              "换了 dtype？）。已按 SUBTRACT_RESIDUAL 处理，但建议回 step1 重弛豫。")
    f0 = f0_raw if as_bool(a.subtract_residual) else np.zeros_like(f0_raw)

    forces3 = compute_set(calc, base, scells, "fc3", a.ckpt, f0)
    ph3.forces = forces3
    write_forces_file(ph3.dataset, forces3, "FORCES_FC3", 3)
    print("[OK] FORCES_FC3（%d 帧）" % len(forces3))

    n2 = 0
    # FC2_SUPERCELL 为空（没给 --dim-fc2）时，phono3py 没有独立的 fc2 超胞矩阵，
    # phonon_supercells_with_displacements 会抛 RuntimeError 而不是返回空——用 try 接住。
    try:
        ph_scells = ph3.phonon_supercells_with_displacements
    except RuntimeError:
        ph_scells = None
    if ph_scells:
        pperfect = ph3.phonon_supercell
        print("[..] fc2 超胞 %d 原子 × %d 个位移（--dim-fc2 生效）"
              % (len(pa_positions(pperfect)), len(ph_scells)))
        base2 = mm.phonopy_atoms_to_ase(pperfect)
        base2.calc = calc
        f0b = np.array(base2.get_forces(), dtype=float)
        if not as_bool(a.subtract_residual):
            f0b = np.zeros_like(f0b)
        forces2 = compute_set(calc, base2, ph_scells, "fc2", a.ckpt, f0b)
        ph3.phonon_forces = forces2
        write_forces_file(ph3.phonon_dataset, forces2, "FORCES_FC2", 2)
        n2 = len(forces2)
        print("[OK] FORCES_FC2（%d 帧）" % n2)

    saved = False
    try:
        ph3.save("phono3py_params.yaml",
                 settings={"force_sets": True, "displacements": True})
        saved = Path("phono3py_params.yaml").is_file()
    except Exception as e:
        print("[WARN] ph3.save 失败（%s）；step3 会退回 phono3py_disp.yaml + FORCES_FC3" % e)
    if saved:
        print("[OK] phono3py_params.yaml（位移+力一体，step3 优先用它）")

    el = time.time() - t0
    summary = {
        "FORCES_DONE": True,
        "n_disp_fc3": int(len(forces3)), "n_disp_fc2": int(n2),
        "n_atoms_supercell": int(natom),
        "residual_force_max_eV_per_A": f0max,
        "residual_subtracted": as_bool(a.subtract_residual),
        "device": dev,
        "params_yaml": bool(saved),
        "model": desc,
        "wall_time_s": el,
        "sec_per_frame": el / max(len(forces3) + n2, 1),
    }
    Path("forces_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    for f in ("forces_fc3.ckpt.npy", "forces_fc2.ckpt.npy"):
        if Path(f).is_file():
            os.remove(f)
    print("[DONE] 取力完成：%d 帧，用时 %.1f min（%.2f s/帧）"
          % (len(forces3) + n2, el / 60.0, summary["sec_per_frame"]))


if __name__ == "__main__":
    main()
