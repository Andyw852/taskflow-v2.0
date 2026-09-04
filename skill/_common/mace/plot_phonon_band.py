#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plot_phonon_band.py —— MACE 声子谱绘图（phonon-mace-cpu/gpu、kl-mace-cpu/gpu 共用）。

读 phonopy band 结构输出 band-dft-cpu.yaml（由 phonon_fit_driver / fc_fit_driver 的
auto_band_structure(write_yaml=True) 生成），画两张图：
  phonon_band_full.png     全频率范围
  phonon_band_lowfreq.png  0~10 THz 低频放大（声学支/低频光学支细节）
并写 phonon_band_summary.json（done_marker）与 band_qpath.txt（路径标签存档）。

run: gen（登录节点跑，不提交作业）。CWD = 材料的技能目录（step3_phonon/ 或 step3_fc/
的兄弟层），band yaml 在下列候选位置探测（与 kl-dft-cpu S5.1 的 _locate 同思路）：
  <技能>/step3_phonon/band-dft-cpu.yaml   (phonon-mace-*)
  <技能>/step3_fc/band-dft-cpu.yaml       (kl-mace-*)
产物写自建的 <技能>/<OUTDIR>/（OUTDIR 由步骤 name 决定，默认 phonon_band_plot）。
"""
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

OUTDIR = os.environ.get("PLOT_OUTDIR", "phonon_band_plot")
LOWF_MAX = 10.0          # 低频放大图上限 THz
FULL_PAD = 3.0           # 全范围图 y 下限余量（显示声学支负频噪声）

# 步骤目录候选（相对 CWD 的兄弟步骤）：phonon-mace 用 step3_phonon，kl-mace 用 step3_fc
SRC_CANDS = ("step3_phonon", "step3_fc")


def _emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


def _clean_label(lb):
    """剥掉 phonopy latex（$..$、\mathrm{} 等）成可读标签。"""
    s = str(lb)
    s = s.replace("$", "")
    s = s.replace("\\mathrm{", "").replace("\\mathbf{", "")
    s = s.replace("{", "").replace("}", "")
    # 下标 _2 -> ₂（可选：保留 ASCII 更稳）
    return s.strip() or "?"


def _locate(cwd):
    """定位 band-dft-cpu.yaml 与本步输出目录。返回 (band_yaml, outdir)。"""
    for sub in SRC_CANDS:
        cand = cwd / sub / "band-dft-cpu.yaml"
        if cand.is_file():
            return cand, cwd / OUTDIR
    # 兜底：cwd 本身是步骤目录
    for sub in ("",):
        cand = cwd / "band-dft-cpu.yaml"
        if cand.is_file():
            return cand, cwd.parent / OUTDIR
    _emit({"status": "error",
           "reason": "缺 band-dft-cpu.yaml（先跑完 S3；已找：%s）"
                     % " | ".join(str(cwd / s / "band-dft-cpu.yaml")
                                  for s in SRC_CANDS)}, 40)


def main():
    cwd = Path.cwd().resolve()
    band_yaml, outdir = _locate(cwd)
    outdir.mkdir(parents=True, exist_ok=True)
    os.chdir(str(outdir))

    with open(str(band_yaml)) as f:
        d = yaml.safe_load(f)

    labels = d.get("labels") or []
    seg_nq = d.get("segment_nqpoint") or []
    phonon = d.get("phonon") or []
    if not phonon:
        _emit({"status": "error", "reason": "%s 无 phonon 数据" % band_yaml}, 40)
    nq = len(phonon)
    nbranch = len(phonon[0].get("band") or [])
    if nbranch == 0:
        _emit({"status": "error", "reason": "%s 无能带频率" % band_yaml}, 40)

    F = np.zeros((nbranch, nq))
    for iq, q in enumerate(phonon):
        for ib, b in enumerate(q.get("band") or []):
            F[ib, iq] = b.get("frequency", 0.0)
    x = np.array([q.get("distance", i) for i, q in enumerate(phonon)])
    bnd = np.cumsum([0] + list(seg_nq)) if seg_nq else np.array([0, nq])

    # 高对称点标注：labels[i]=[段起点,段终点]，终点 x = bnd[i+1]
    tick_pos, tick_lab = [], []
    if labels:
        for i in range(len(labels)):
            if i == 0:
                tick_pos.append(bnd[0])
                tick_lab.append(labels[0][0])
            if i + 1 < len(bnd):
                tick_pos.append(bnd[i + 1])
                tick_lab.append(labels[i][1])
    else:
        tick_pos, tick_lab = [0, nq - 1], ["Γ", "X"]

    def _draw(fname, title, ylo, yhi):
        fig, ax = plt.subplots(figsize=(12, 8), dpi=150)
        for i in range(nbranch):
            ax.plot(x, F[i], color="#1f77b4", lw=0.5, alpha=0.85)
        for tp in tick_pos:
            ax.axvline(x[min(int(tp), nq - 1)], color="gray", ls="--", lw=0.6)
        ax.axhline(0, color="k", lw=0.8)
        ax.set_xticks([x[min(int(tp), nq - 1)] for tp in tick_pos])
        ax.set_xticklabels([_clean_label(lb) for lb in tick_lab], fontsize=11)
        ax.set_ylim(ylo, yhi)
        ax.set_xlim(x[0], x[-1])
        ax.set_ylabel("Frequency (THz)", fontsize=13)
        ax.set_xlabel("Wave vector", fontsize=13)
        ax.set_title(title, fontsize=14)
        fig.tight_layout()
        fig.savefig(fname, dpi=150)
        plt.close(fig)

    fmin = float(F.min())
    fmax_full = float(F.max())
    # 全范围：上限 = 最大频率 + 10% 余量
    full_hi = fmax_full * 1.05 + 2.0
    full_lo = min(-FULL_PAD, fmin - FULL_PAD if fmin < 0 else -FULL_PAD)
    _draw("phonon_band_full.png",
          "Phonon dispersion (full range, %.1f-%.1f THz)" % (full_lo, full_hi),
          full_lo, full_hi)
    _draw("phonon_band_lowfreq.png",
          "Phonon dispersion (low frequency, 0-%.0f THz)" % LOWF_MAX,
          -0.5, LOWF_MAX)

    summary = {
        "status": "ok",
        "band_yaml": str(band_yaml),
        "out_dir": str(outdir),
        "n_qpoint": nq,
        "n_branch": nbranch,
        "min_freq_THz": round(float(fmin), 6),
        "max_freq_THz": round(float(fmax_full), 6),
        "imaginary": bool(fmin < -0.05),
        "plots": ["phonon_band_full.png", "phonon_band_lowfreq.png"],
    }
    (outdir / "phonon_band_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n")
    # 路径标签存档（供后续查图对应关系）
    (outdir / "band_qpath.txt").write_text(
        "\n".join("  %s -- %s" % (_clean_label(a), _clean_label(b))
                   for a, b in labels) if labels else "no labels",
        encoding="utf-8")
    print("[OK] %s / %s  min_freq=%.3f THz%s"
          % ("phonon_band_full.png", "phonon_band_lowfreq.png",
             fmin, "  ⚠️虚频" if fmin < -0.05 else ""))
    _emit(summary, 0)


if __name__ == "__main__":
    main()
