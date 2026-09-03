#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_band_refs.py —— 从 band 技能输出自动补齐 energies.json 的物理量。

读 band_summary.json（E_gap/VBM/CBM），从 band.dat 拟合带边有效质量 mstar，
epsilon 取 step.conf 默认（文献值；DFPT 介电张量是更准来源，可后补）。

用法（S4 自动调用；也可手动）：python3 extract_band_refs.py [band_summary.json]
"""
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

HBAR2_2ME = 3.8099822    # hbar^2/(2 m_e) 单位 eV·Å^2

def load_band_summary(path):
    if not os.path.exists(path):
        raise SystemExit("[错误] 找不到 band_summary.json：%s" % path)
    s = json.load(open(path, encoding="utf-8"))
    return s

def fit_mstar(band_dat, is_valence):
    """从 band.dat 拟合带边有效质量 m*/m_e。

    band.dat：第1列 k-distance(1/Å)，其余列 E−E_fermi(eV)。
    is_valence=True 拟合 VBM（最高占据带），False 拟合 CBM（最低空带）。
    用带边跟踪（每个 k 取 VBM/CBM），在极值附近拟合 E(k)=E0+A·(k−k0)^2，
    m* = hbar^2/(2m_e) / A。拟合失败返回 None。"""
    rows = []
    for line in open(band_dat, errors="ignore"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split()
        try:
            rows.append((float(p[0]), [float(x) for x in p[1:]]))
        except (ValueError, IndexError):
            continue
    if len(rows) < 5:
        return None
    edge = []
    for k, es in rows:
        if is_valence:
            occ = [e for e in es if e <= 0.0]
            edge.append(max(occ) if occ else 0.0)
        else:
            unocc = [e for e in es if e > 0.0]
            edge.append(min(unocc) if unocc else 0.0)
    # 极值点：VBM 取最大值，CBM 取最小值
    i0 = max(range(len(edge)), key=lambda i: edge[i]) if is_valence else          min(range(len(edge)), key=lambda i: edge[i])
    k0 = rows[i0][0]
    # 在极值附近取窗口（左右各取到 |k-k0| 最大处，最多 8 点）
    pts = [(abs(rows[i][0] - k0), edge[i]) for i in range(len(rows))]
    pts = [p for p in pts if p[0] < 0.15]     # 0.15 Å^-1 窗口，避免跨过能带转折
    pts.sort()
    if len(pts) < 3:
        return None
    # 最小二乘拟合 E = E0 + A * dk^2
    n = len(pts)
    Sx = sum(p[0] for p in pts)
    Sx2 = sum(p[0]**2 for p in pts)
    Sx4 = sum(p[0]**4 for p in pts)
    Sy = sum(p[1] for p in pts)
    Sx2y = sum(p[0]**2 * p[1] for p in pts)
    det = n*Sx4 - Sx2*Sx2
    if abs(det) < 1e-12:
        return None
    A = (n*Sx2y - Sx2*Sy) / det
    if A <= 1e-4:
        return None
    return HBAR2_2ME / A

def vbm_cbm_from_eigenval(path):
    """从 EIGENVAL 提取 VBM/CBM 本征值（绝对值，VASP 内部参考系）。SOC 单自旋通道。

    这是缺陷超胞同一套 PBE+SOC 设置下 bulk 超胞的 VBM，用于形成能的电子库对齐；
    band_summary 里的 VBM 是 HSE/原胞/能带图的，不能直接复用。"""
    if not os.path.exists(path):
        return None, None
    lines = open(path, errors="ignore").read().splitlines()
    if len(lines) < 7:
        return None, None
    p = lines[5].split()
    nkpts, nbands = int(p[1]), int(p[2])
    idx = 6
    vbm, cbm = None, None
    for _ in range(nkpts):
        while idx < len(lines) and lines[idx].strip() == "":
            idx += 1
        if idx >= len(lines):
            break
        idx += 1  # 跳过 k 点坐标行
        for _b in range(nbands):
            if idx >= len(lines):
                break
            tok = lines[idx].split()
            idx += 1
            if len(tok) < 3:
                continue
            e, occ = float(tok[1]), float(tok[2])
            if occ > 0.5:
                vbm = e if vbm is None or e > vbm else vbm
            else:
                cbm = e if cbm is None or e < cbm else cbm
    return vbm, cbm

def _auto_detect_band_summary():
    """按材料名自动探测 band_summary.json（band 技能在 joint_research_project 下）。"""
    material = os.path.basename(os.path.dirname(os.path.abspath(os.getcwd())))
    cands = [
        "/public/home/wangchao/joint_research_project/huang/%s/POSCAR_B/band/step4_band_plot/band_summary.json" % material,
        "/public/home/wangchao/joint_research_project/huang/%s/POSCAR_B/band/step3_band_plot/band_summary.json" % material,
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return ""

def main(path=None):
    conf = D.load_stepconf()
    if path is None:
        path = conf.get("BAND_SUMMARY", "")
    if not path:
        path = _auto_detect_band_summary()
    if not path:
        raise SystemExit("[错误] 找不到 band_summary.json（请在 step.conf 设 BAND_SUMMARY，或确认 band 步输出路径）")
    s = load_band_summary(path)
    E_gap = float(s.get("gap_eV"))
    VBM = s.get("vbm", {}).get("E_eV")
    CBM = s.get("cbm", {}).get("E_eV")
    # mstar：优先 band.dat 拟合，失败退 config 默认
    band_dat = s.get("files", {}).get("dat", "")
    mstar_e = fit_mstar(band_dat, False) if band_dat else None
    mstar_h = fit_mstar(band_dat, True) if band_dat else None
    mstar_e = mstar_e if mstar_e and 0.005 < mstar_e < 10 else float(conf.get("MSTAR_E", 0.2))
    mstar_h = mstar_h if mstar_h and 0.005 < mstar_h < 10 else float(conf.get("MSTAR_H", 0.2))
    eps = float(conf.get("EPSILON", 100.0))
    # bulk 超胞 VBM 本征值（PBE+SOC，与缺陷超胞同设置；band_summary 的 VBM 是 HSE/原胞，仅作参考）
    vbm_bulk, cbm_bulk = vbm_cbm_from_eigenval("step1_bulk/EIGENVAL")
    # 写入 energies.json（合并已有的 mu）
    ref = {}
    if os.path.exists("energies.json"):
        ref = json.load(open("energies.json", encoding="utf-8"))
    ref.update({"E_gap": E_gap, "VBM": VBM, "CBM": CBM,
                "epsilon": eps, "mstar_e": round(mstar_e, 4), "mstar_h": round(mstar_h, 4)})
    if vbm_bulk is not None:
        ref["VBM_bulk_abs"] = vbm_bulk
        ref["CBM_bulk_abs"] = cbm_bulk
    json.dump(ref, open("energies.json", "w"), indent=2, ensure_ascii=False)
    print("[OK] E_gap=%.4f eV  epsilon=%.1f  mstar_e=%.3f  mstar_h=%.3f -> energies.json"
          % (E_gap, eps, mstar_e, mstar_h))
    print("    （VBM=%.4f CBM=%.4f eV，来自 %s）" % (VBM or 0.0, CBM or 0.0, os.path.basename(path)))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
