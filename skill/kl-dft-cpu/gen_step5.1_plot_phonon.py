#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step5.1_plot_phonon.py —— S5.1 声子谱绘图（run: gen，不提交作业）。

读 step5_fc/phono3py 里拟合好的 fc2.hdf5 画声子谱（参考用户 plot_phonon_band.py）。
结构/超胞/原胞矩阵从 phono3py_disp.yaml 直接取（与拟合、虚频闸完全一致，避免 SPOSCAR 反推）。

【kl-dft-cpu-s51 路径修复】tf 的 remote_gen 是 `cd <材料>/kl-dft-cpu && python gen_step5.1_plot_phonon.py`，
CWD = 材料的技能目录（step5_fc/ 的兄弟层），不是步骤目录。原来写死 `../step5_fc/phono3py`
会跳到上一级，必然找不到 fc2.hdf5；而产物又写在 CWD，tf 去 step5_phonon_plot/ 找
done_marker 也必然落空（两个 bug 叠在一起）。
现在改成：
  · 输入目录 FCDIR 在几个候选位置里探测 fc2.hdf5（兼容 CWD=材料目录 / 步骤目录 / step5_fc）；
  · 自建 step5_phonon_plot/ 并 chdir 进去，所有产物（png/yaml/summary）都落在步骤目录里。
与 band-dft-cpu 技能的 gen_step3.1_plot_band.py 同一套约定：裸名访问兄弟目录、产物写自建目录。

NAC 规则（按用户要求）：
  · 目录里有 BORN（材料考虑了 NAC）→ 画两张，命名区分：band_nac.* 和 band_nonac.*；
  · 没有 BORN → 只画 band_nonac.*。
注意：2D 材料的 3D-NAC 在近 Γ 会有 LO-TO 假象，两张对照正好能看出 NAC 的影响
（虚频闸判据对 2D 用的是无 NAC，见 kl_fc_backends._stability_gate）。

产物（全部写入 step5_phonon_plot/）：
  band_nac.png/yaml、band_nonac.png/yaml（视有无 BORN）、phonon_plot_summary.json（done_marker）。
"""
import json
import os
import sys
from pathlib import Path

OUT_NAME = "step5_phonon_plot"       # 步骤目录名，必须与 skill.yaml 的 name 一致
FCSUB    = "phono3py"                # S5 拟合产物子目录
NPOINTS  = 101                       # 每段路径采样点
IMAG_TOL = -0.05                     # THz，低于此判虚频（容数值噪声）

FCDIR = None                         # 由 _locate() 定为绝对路径


def _emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


def _locate():
    """定位 S5 产物目录与本步输出目录，返回 (fcdir, outdir)，都是绝对路径。

    候选按 CWD 的三种可能排列（第一个能看到 fc2.hdf5 的胜出）：
      A. CWD = 材料技能目录 <mat>/kl-dft-cpu      —— tf remote_gen 的实际行为
      B. CWD = <mat>/kl-dft-cpu/step5_phonon_plot —— 手动进步骤目录里跑
      C. CWD = <mat>/kl-dft-cpu/step5_fc          —— 手动进 S5 目录里跑
    """
    cwd = Path.cwd().resolve()
    cands = [(cwd / "step5_fc" / FCSUB,        cwd / OUT_NAME),
             (cwd.parent / "step5_fc" / FCSUB, cwd if cwd.name == OUT_NAME
                                               else cwd.parent / OUT_NAME),
             (cwd / FCSUB,                     cwd.parent / OUT_NAME)]
    for fc, out in cands:
        if (fc / "fc2.hdf5").is_file():
            return fc.resolve(), out
    _emit({"status": "error",
           "reason": "缺 fc2.hdf5，先把 S5_fc 跑完（已查找：%s）"
                     % " | ".join(str(fc / "fc2.hdf5") for fc, _ in cands)}, 40)


def _build_phonopy():
    """从 phono3py_disp.yaml 建 Phonopy + 读 fc2.hdf5（每次新建，避免 NAC 状态串台）。"""
    import numpy as np
    import phono3py
    from phono3py.file_IO import read_fc2_from_hdf5
    from phonopy import Phonopy
    ph3 = phono3py.load(str(FCDIR / "phono3py_disp.yaml"),
                        produce_fc=False, is_nac=False, log_level=0)
    ph = Phonopy(ph3.unitcell, supercell_matrix=ph3.supercell_matrix,
                 primitive_matrix=ph3.primitive_matrix)
    ph.force_constants = np.asarray(read_fc2_from_hdf5(filename=str(FCDIR / "fc2.hdf5")))
    return ph


def _set_nac(ph, on):
    if on:
        from phonopy.file_IO import parse_BORN
        nac = parse_BORN(ph.primitive, filename=str(FCDIR / "BORN"))
        if isinstance(nac, dict) and not nac.get("factor"):
            nac["factor"] = 14.399652     # VASP NAC 单位换算因子（phonopy-vasp-born 常缺）
        ph.nac_params = nac
    else:
        ph.nac_params = None              # 显式建"无 NAC"动力学矩阵


def _band_path(ph):
    """高对称路径：优先 seekpath 自动，失败退回通用路径。"""
    from phonopy.phonon.band_structure import get_band_qpoints_and_path_connections
    try:
        from phonopy.phonon.band_structure import get_band_qpoints_by_seekpath
        bands, labels, connections = get_band_qpoints_by_seekpath(ph.primitive, NPOINTS)
        return bands, connections, labels, "seekpath"
    except Exception as e:
        print("[..] seekpath 不可用（%s），用通用路径 Γ-M-K-Γ" % type(e).__name__)
        paths = [[[0, 0, 0], [0.5, 0, 0]], [[0.5, 0, 0], [1./3, 1./3, 0]],
                 [[1./3, 1./3, 0], [0, 0, 0]]]
        bands, connections = get_band_qpoints_and_path_connections(paths, npoints=NPOINTS)
        return bands, connections, ["$\\Gamma$", "M", "K", "$\\Gamma$"], "fallback"


def _plot(tag):
    """tag='nac' | 'nonac'：画一张声子谱，返回摘要。"""
    import matplotlib
    matplotlib.use("Agg")
    ph = _build_phonopy()
    _set_nac(ph, tag == "nac")
    bands, conn, labels, src = _band_path(ph)
    ph.run_band_structure(bands, path_connections=conn, labels=labels,
                          with_eigenvectors=False)
    ph.write_yaml_band_structure(filename="band_%s.yaml" % tag)
    plt = ph.plot_band_structure()
    plt.savefig("band_%s.png" % tag, dpi=200, bbox_inches="tight")
    try:
        plt.close("all")
    except Exception:
        pass
    bs = ph.get_band_structure_dict()
    fmin = float(min(fr.min() for fr in bs["frequencies"]))
    print("[OK] band_%s.png / band_%s.yaml  路径=%s  最低频率=%.3f THz%s"
          % (tag, tag, src, fmin, "  ⚠️含虚频" if fmin < IMAG_TOL else ""))
    return {"tag": tag, "nac": tag == "nac", "png": "band_%s.png" % tag,
            "yaml": "band_%s.yaml" % tag, "min_freq_THz": round(fmin, 4),
            "imaginary": fmin < IMAG_TOL, "path_source": src}


def main():
    global FCDIR
    FCDIR, outdir = _locate()
    outdir.mkdir(parents=True, exist_ok=True)
    os.chdir(str(outdir))                 # 之后所有相对写入都落在步骤目录里
    out = Path.cwd()
    print("[..] fc 源：%s\n[..] 产物目录：%s" % (FCDIR, out))

    has_born = (FCDIR / "BORN").is_file()
    tags = ["nac", "nonac"] if has_born else ["nonac"]
    print("[..] NAC=%s → 输出 %s" % ("有(BORN)" if has_born else "无",
                                     " + ".join("band_%s" % t for t in tags)))

    plots = []
    for t in tags:
        try:
            plots.append(_plot(t))
        except Exception as e:
            print("[WARN] band_%s 绘制失败：%s" % (t, e))
    if not plots:
        _emit({"status": "error", "reason": "声子谱全部失败（见上 WARN）"}, 40)

    summary = {"status": "ok", "nac_considered": has_born,
               "fc_dir": str(FCDIR), "out_dir": str(out),
               "note": ("有 NAC：band_nac + band_nonac 两张对照"
                        if has_born else "无 NAC：仅 band_nonac"),
               "plots": plots}
    (out / "phonon_plot_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] S5.1 声子谱：%s" % ", ".join(p["png"] for p in plots))
    _emit(summary, 0)


if __name__ == "__main__":
    main()
