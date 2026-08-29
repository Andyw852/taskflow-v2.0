#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step3.1_plot_band.py
========================
在父目录（含 step3_PBE_WAVECAR/）下运行，搭建并完成 step3_band_plot：
提取 PBE / PBE+D3 / PBEsol (+SOC) 能带、计算带隙、绘图。

与 gen_step4.1_plot_band.py 是同一套逻辑，只是数据源换成 step3（半局域泛函，非 HSE）。
数据源有两种，load_path_bands() 自动识别（★ 这段说明已随 gen_step3_WAVECAR.py
改用 KPOINTS_OPT 而更新）：
  (A) KPOINTS_OPT 方案（现在的默认，USE_KPOINTS_OPT=True）
      KPOINTS 里只有均匀加权网格，路径点在 KPOINTS_OPT 里、自洽后 one-shot。
      => EIGENVAL 里【只有网格点，没有路径点】；路径本征值唯一的纯文本出口是
         vasprun.xml 的 <eigenvalues_kpoints_opt> 块，所以这个目录必须有 vasprun.xml。
  (B) 零权重旧方案（USE_KPOINTS_OPT=False）
      路径点以 weight=0 混在 KPOINTS 里 => 直接从 EIGENVAL 挑 weight==0 的点。
两种情况都不需要再跑一次 ICHARG=11。

做的事：
    1. 新建 step3_band_plot/，把 step3 的输入拷贝进来（自包含、可追溯）：
           EIGENVAL, POSCAR, INCAR, KPOINTS, workflow_method.txt
       （OUTCAR 太大不拷，只提取 E-fermi 记入摘要。）
    2. 读 EIGENVAL：按 k 点权重分离"均匀网格点(权重>0)"与"能带路径点(权重=0)"，
       只用后者画能带。
    3. 读 POSCAR 晶格 -> 倒格子，计算路径点累积 k 距离作为横轴；
       段间跳变（如 A|L、K|H）自动识别为断点，不计入距离。
    4. 高对称点标签：按六方 3D 分数坐标匹配（与 gen_step3_WAVECAR 一致），
       非六方体系可用 --labels 手动指定。
    5. 带隙：SOC 下每条带占 1 个电子；VBM = 第 NELECT 条带最大值，
       CBM = 第 NELECT+1 条带最小值；同 k 点 -> 直接带隙，否则间接。
    6. 输出（全部写入 step3_band_plot/）：
           band-dft-cpu.dat / band-dft-cpu.png / band_klabels.txt / band_summary.json

agent 约定：stdout 只输出一行 JSON；stderr 是过程日志；
           退出码 0=成功  40=错误。

用法：
    cd <父目录>      # 里面有 step3_PBE_WAVECAR/（已跑完）
    python gen_step3.1_plot_band.py
    python gen_step3.1_plot_band.py --emin -2 --emax 3
    python gen_step3.1_plot_band.py --labels "G,0,0,0;M,0.5,0,0;..."

依赖: numpy, matplotlib
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

# ============================== 配置 ==============================
STEP3_DIR = "step2_bandgap/step2.2_pbe"    # 源目录
OUT_DIR   = "step2_bandgap/step2.2_pbe_plot"     # 目标目录（输入输出都在这里）

# 从 step3 拷贝过来的输入（存在才拷；OUTCAR 太大只提取 E-fermi）
COPY_INPUTS = ["EIGENVAL", "vasprun.xml", "POSCAR", "INCAR", "KPOINTS",
               "KPOINTS_OPT", "kpath.json", "workflow_method.txt"]   # 存在才拷
# vasprun.xml: KPOINTS_OPT 方案下，能带路径的本征值【只】写在这里面
#              （VASP 不产出 EIGENVAL_OPT——官方只保证 PROCAR_OPT / vaspout.h5）

# ---- 六方 3D 高对称点（与 gen_step3_WAVECAR.py 完全一致）----
KPT_COORDS = {
    "G": (0.0,     0.0,     0.0),
    "M": (0.5,     0.0,     0.0),
    "K": (1.0 / 3, 1.0 / 3, 0.0),
    "A": (0.0,     0.0,     0.5),
    "L": (0.5,     0.0,     0.5),
    "H": (1.0 / 3, 1.0 / 3, 0.5),
}
LABEL_TOL = 1e-3      # 分数坐标匹配容差
JUMP_FACTOR = 3.0     # 相邻路径点间距 > 中位数的该倍数 -> 段间跳变

# 泛函名（用于图标题/摘要）：优先读 workflow_method.txt 的 FUNC=，回退嗅探 INCAR
SUPPORTED_FUNCS = ("pbe-d3", "pbesol", "pbe")
FUNC_PRETTY = {"pbe-d3": "PBE+D3", "pbesol": "PBEsol", "pbe": "PBE"}
# =================================================================


MESH_GAP_TOL = 0.01   # eV，自洽网格 vs 路径带隙的容差，超过就告警


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 解析输入文件
# ---------------------------------------------------------------------------
def read_lattice(poscar: Path):
    lines = poscar.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    scale = float(lines[1].split()[0])
    vecs = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)])
    if scale < 0:
        vol = abs(np.linalg.det(vecs))
        scale = (abs(scale) / vol) ** (1.0 / 3.0)
    return vecs * scale


def read_eigenval(path: Path):
    """
    返回 (nelect, kpts, weights, bands)
      kpts   : (nk, 3) 分数坐标
      weights: (nk,)
      bands  : (nk, nbands)         ISPIN=1（含 SOC 非共线）
               (nk, nbands, 2)      ISPIN=2（自旋极化；最后一维 0=↑, 1=↓）

    ISPIN=2 时 VASP 把上下自旋写在同一行：`band  E_up  E_down  occ_up  occ_down`，
    所以按列 1/2 分别取，而不是两个 k 点块。
    """
    lines = path.read_text(errors="ignore").splitlines()
    ispin = int(lines[0].split()[3])
    nelect, nk, nb = (int(float(x)) for x in lines[5].split()[:3])

    kpts, weights, bands_up, bands_dn = [], [], [], []
    i = 6
    for _ in range(nk):
        while i < len(lines) and not lines[i].strip():
            i += 1
        t = lines[i].split()
        kpts.append([float(t[0]), float(t[1]), float(t[2])])
        weights.append(float(t[3]))
        i += 1
        eb_up, eb_dn = [], []
        for _ in range(nb):
            parts = lines[i].split()
            eb_up.append(float(parts[1]))
            if ispin == 2:
                eb_dn.append(float(parts[2]))
            i += 1
        bands_up.append(eb_up)
        if ispin == 2:
            bands_dn.append(eb_dn)
    if ispin == 2:
        bands = np.stack([bands_up, bands_dn], axis=2)   # (nk, nb, 2)
    else:
        bands = np.array(bands_up)
    return nelect, np.array(kpts), np.array(weights), bands


def _flatten_spins(bands):
    """(nk, nb) → (nk, nb)； (nk, nb, 2) → (nk, 2*nb)（每个 k 点跨自旋排序）。

    带隙判据用：自旋极化下 VBM/CBM 要跨上下自旋比较，先把每个 k 点的
    2*nb 个本征值按能量升序排好，第 nocc 个即 VBM、第 nocc+1 个即 CBM。
    """
    if bands.ndim == 3:
        return np.sort(bands.reshape(bands.shape[0], -1), axis=1)
    return bands


def read_kpoints_list(path: Path):
    """读显式列表格式的 KPOINTS/KPOINTS_OPT，返回 (nk,3) 分数坐标。"""
    lines = [ln for ln in path.read_text(errors="ignore").splitlines()]
    n = int(lines[1].split()[0])
    out = []
    for ln in lines[3:]:
        t = ln.split()
        if len(t) >= 3:
            out.append([float(t[0]), float(t[1]), float(t[2])])
        if len(out) == n:
            break
    return out


def read_incar_flag(incar: Path, key: str):
    if not incar.exists():
        return None
    for line in incar.read_text(errors="ignore").splitlines():
        m = re.match(rf"\s*{key}\s*=\s*(\S+)", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def read_efermi(outcar: Path):
    """取【自洽阶段】的 E-fermi。

    ★ 不能简单地取最后一个：KPOINTS_OPT 的 one-shot 阶段结束后 VASP 会再打印
      一次 E-fermi，那个值是拿【高对称路径上的点】算出来的。路径不是布里渊区的
      采样（没有权重、只覆盖若干条线），由它算出的费米能没有物理意义，用来当
      能量零点会把整张图平移。
      OUTCAR 里 one-shot 阶段以 " Start KPOINTS_OPT (optional k-point list driver)"
      开头，这里扫到该行就停，返回它之前最后一个 E-fermi（即自洽网格上的那个）。
    """
    if not outcar.exists():
        return None
    ef = None
    for line in outcar.read_text(errors="ignore").splitlines():
        if "KPOINTS_OPT" in line and "Start" in line:
            break
        m = re.search(r"E-fermi\s*:\s*(-?[\d.]+)", line)
        if m:
            ef = float(m.group(1))
    return ef


def mesh_gap_check(eigenval: Path, nocc: int, evbm: float, ecbm: float):
    """用【自洽网格】上的本征值交叉检验路径带隙。

    KPOINTS_OPT 方案下 EIGENVAL 里装的是自洽用的均匀网格点（权重>0）。
    它们与路径点用的是同一套哈密顿量（HSE 就是 HSE 的），所以本征值同样有效。
    如果真正的 VBM/CBM 落在网格点上、而路径（尤其被 --line-density 降采样过的
    路径）恰好没采到那个 k，只看路径就会把带隙报大——图上完全看不出来。

    返回 (info_dict, warn_str_or_None)；EIGENVAL 缺失或没有加权点时返回 (None, None)。
    """
    try:
        if not eigenval.exists() or eigenval.stat().st_size == 0:
            return None, None
        _, kpts, weights, bands = read_eigenval(eigenval)
    except Exception:
        return None, None
    mask = weights > 0
    if mask.sum() == 0:
        return None, None
    eflat = _flatten_spins(bands[mask])           # ISPIN=2 每个 k 点跨自旋排序
    if eflat.shape[1] < nocc + 1:
        return None, None
    mvb, mcb = eflat[:, nocc - 1], eflat[:, nocc]
    im, ic = int(np.argmax(mvb)), int(np.argmin(mcb))
    mesh_vbm, mesh_cbm = float(mvb[im]), float(mcb[ic])
    comb_vbm = max(evbm, mesh_vbm)
    comb_cbm = min(ecbm, mesh_cbm)
    info = {
        "n_mesh_kpts": int(mask.sum()),
        "mesh_vbm_eV": round(mesh_vbm, 4),
        "mesh_cbm_eV": round(mesh_cbm, 4),
        "mesh_vbm_k_frac": [round(float(v), 6) for v in kpts[mask][im]],
        "mesh_cbm_k_frac": [round(float(v), 6) for v in kpts[mask][ic]],
        "gap_mesh_eV": round(mesh_cbm - mesh_vbm, 4),
        "gap_path_plus_mesh_eV": round(comb_cbm - comb_vbm, 4),
    }
    path_gap = ecbm - evbm
    delta = path_gap - (comb_cbm - comb_vbm)
    if delta > MESH_GAP_TOL:
        who = []
        if mesh_vbm > evbm + MESH_GAP_TOL:
            who.append("VBM 在网格点 %s 上更高 (%.4f > %.4f)"
                       % (info["mesh_vbm_k_frac"], mesh_vbm, evbm))
        if mesh_cbm < ecbm - MESH_GAP_TOL:
            who.append("CBM 在网格点 %s 上更低 (%.4f < %.4f)"
                       % (info["mesh_cbm_k_frac"], mesh_cbm, ecbm))
        warn = ("自洽网格给出的带隙比路径小 %.4f eV —— %s。"
                "说明高对称路径没采到真正的带边，报出的路径带隙偏大。"
                "对策：把该 k 点加进 step3 的 EXTRA_KPTS，或调大 --line-density 重跑。"
                % (delta, "；".join(who)))
        info["verdict"] = "path_misses_extremum"
        return info, warn
    info["verdict"] = "consistent"
    return info, None


def detect_func(method_file: Path, incar: Path):
    """优先 workflow_method.txt 的 FUNC=；否则嗅探 INCAR 的 GGA / IVDW。"""
    if method_file.exists():
        for line in method_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("FUNC="):
                func = line.split("=", 1)[1].strip()
                if func in SUPPORTED_FUNCS:
                    return func
                break
    gga = (read_incar_flag(incar, "GGA") or "").upper().strip('"\'')
    ivdw = read_incar_flag(incar, "IVDW")
    if gga.startswith("PS"):
        return "pbesol"
    if ivdw and ivdw.split()[0] in ("11", "12"):
        return "pbe-d3"
    return "pbe"


# ---------------------------------------------------------------------------
# 路径几何：累积距离 + 段间跳变 + 高对称点标签
# ---------------------------------------------------------------------------
def build_axis(kfrac, lat, label_map):
    """
    返回 (x, breaks, ticks)
      x     : 每个路径点的累积 k 距离（跳变不计距离）
      breaks: 跳变处的 x 位置（画竖线）
      ticks : [(x, label), ...] 高对称点刻度
    """
    recip = 2 * np.pi * np.linalg.inv(lat).T
    cart = kfrac @ recip
    seg = np.linalg.norm(np.diff(cart, axis=0), axis=1)
    med = np.median(seg[seg > 1e-10]) if np.any(seg > 1e-10) else 1.0

    x = np.zeros(len(kfrac))
    breaks = []
    for i, d in enumerate(seg, start=1):
        if d > JUMP_FACTOR * med:          # 段间跳变：不累积距离
            x[i] = x[i - 1]
            breaks.append(x[i - 1])
        else:
            x[i] = x[i - 1] + d

    # 标签：分数坐标匹配（模 1 归一后比较）
    ticks = []
    for i, f in enumerate(kfrac):
        for lab, ref in label_map.items():
            diff = np.array(f) - np.array(ref)
            diff -= np.round(diff)
            if np.linalg.norm(diff) < LABEL_TOL:
                disp = "Γ" if lab == "G" else lab
                # 同一 x 处重复标签（段首尾相接/跳变）合并为 A|L 形式
                if ticks and abs(ticks[-1][0] - x[i]) < 1e-8:
                    if disp not in ticks[-1][1].split("|"):
                        ticks[-1] = (ticks[-1][0], ticks[-1][1] + "|" + disp)
                else:
                    ticks.append((x[i], disp))
                break
    return x, breaks, ticks


def build_axis_from_kpath(kfrac, lat, kpath_meta):
    """
    用 step3 写出的 kpath.json 直接按【索引】给标签、按记录的 breaks 断段——
    不做任何分数坐标匹配，因此对任何晶系都成立。
    返回 (x, breaks_x, ticks) 或 None（元数据与 EIGENVAL 对不上时）。
    """
    pts = kpath_meta.get("kpoints") or []
    labs = kpath_meta.get("point_labels") or []
    if len(pts) != len(kfrac) or len(labs) != len(kfrac):
        log(f"[warn] kpath.json 有 {len(pts)} 个路径点，但本征值文件里有 {len(kfrac)} 个——"
            "两者不匹配，退回坐标匹配模式")
        return None
    d = np.abs(np.asarray(pts, dtype=float) - np.asarray(kfrac, dtype=float)).max()
    if d > 1e-4:
        log(f"[warn] kpath.json 与本征值文件的 k 点坐标最大偏差 {d:.2e}，退回坐标匹配模式")
        return None

    recip = 2 * np.pi * np.linalg.inv(lat).T
    cart = np.asarray(kfrac, dtype=float) @ recip
    seg = np.linalg.norm(np.diff(cart, axis=0), axis=1)
    jump_at = set(int(i) for i in kpath_meta.get("breaks", []))

    x = np.zeros(len(kfrac))
    breaks_x = []
    for i in range(1, len(kfrac)):
        if i in jump_at:                      # 段间跳变：不累积距离
            x[i] = x[i - 1]
            breaks_x.append(x[i])
        else:
            x[i] = x[i - 1] + seg[i - 1]

    ticks = []
    for i, lab in enumerate(labs):
        if not lab:
            continue
        if ticks and abs(ticks[-1][0] - x[i]) < 1e-8:
            if lab not in ticks[-1][1].split("|"):
                ticks[-1] = (ticks[-1][0], ticks[-1][1] + "|" + lab)
        else:
            ticks.append((x[i], lab))
    return x, breaks_x, ticks


def parse_manual_labels(spec):
    """--labels "G,0,0,0;M,0.5,0,0" -> dict"""
    out = {}
    for item in spec.split(";"):
        t = [x.strip() for x in item.split(",")]
        if len(t) != 4:
            raise ValueError(f"--labels 格式错误: {item!r}")
        out[t[0]] = (float(t[1]), float(t[2]), float(t[3]))
    return out


def read_vasprun_kpoints_opt(path: Path):
    """
    从 vasprun.xml 读 KPOINTS_OPT 的 one-shot 本征值。

    VASP 6.3+ 的 KPOINTS_OPT 结果【不写 EIGENVAL_OPT】（官方只保证 PROCAR_OPT
    和 vaspout.h5 的 kpoints_opt 字段），纯文本用户唯一的可靠出口就是 vasprun.xml：
        <kpoints comment="kpoints_opt">        -> <varray name="kpointlist">
        <eigenvalues_kpoints_opt>              -> <eigenvalues><array><set>
                                                    <set comment="spin 1">
                                                      <set comment="kpoint 1"><r>E occ</r>...
    返回 (kpts, bands) 或 None（文件里没有 kpoints_opt 块）。
    用 iterparse 流式解析：vasprun.xml 动辄几百 MB，不能整份读进内存。
    """
    import xml.etree.ElementTree as ET

    kpts, bands = None, None
    keep = 0          # >0 表示当前在"需要保留的子树"内部，不能 clear
    try:
        for event, elem in ET.iterparse(str(path), events=("start", "end")):
            is_target = (elem.tag == "eigenvalues_kpoints_opt" or
                         (elem.tag == "kpoints" and elem.get("comment") == "kpoints_opt"))
            if event == "start":
                if is_target:
                    keep += 1
                continue

            if is_target:
                keep -= 1
                if elem.tag == "kpoints":
                    va = elem.find("varray[@name='kpointlist']")
                    if va is not None:
                        kpts = [[float(x) for x in v.text.split()] for v in va.findall("v")]
                else:
                    arr = elem.find("eigenvalues/array/set")
                    if arr is not None:
                        spins = arr.findall("set")
                        if spins:
                            chans = [np.array(
                                [[float(r.text.split()[0]) for r in ks.findall("r")]
                                 for ks in sp.findall("set")], dtype=float)
                                for sp in spins]
                            # ISPIN=1 一个自旋；ISPIN=2 上下两个自旋 → (nk, nb, 2)
                            bands = chans[0] if len(chans) == 1 else np.stack(chans, axis=2)
                elem.clear()
            elif keep == 0:
                elem.clear()          # 丢掉不关心的部分，控制内存
    except ET.ParseError as exc:
        raise ValueError(f"vasprun.xml 解析失败（作业可能被中途杀掉）: {exc}")

    if bands is None:
        return None
    return kpts, bands


def load_path_bands(d: Path):
    """
    读出"能带路径上的"本征值，自动兼容两种方案：

      (A) KPOINTS_OPT 方案（VASP >= 6.3，推荐）
          自洽只在 KPOINTS 的均匀加权点上做；路径点放在 KPOINTS_OPT 里，
          自洽收敛后 VASP 再做一次 one-shot。
          ★ 注意：one-shot 的本征值【不写进 EIGENVAL，也不写 EIGENVAL_OPT】——
            EIGENVAL 里只有自洽用的那些加权网格点。唯一的纯文本出口是
            vasprun.xml 的 <eigenvalues_kpoints_opt> 块。

      (B) 零权重方案（旧，任何版本）
          路径点以 weight=0 混在 KPOINTS 里，每个电子步都被重算（HSE 下极贵）。
          -> 从 EIGENVAL 里挑 weight==0 的点。

    返回 (nelect, kpath, epath, nk_total, source)
    """
    # NELECT / NBANDS 永远从主 EIGENVAL 拿（两种方案下它都在）
    nelect = kpts = weights = bands = None
    if (d / "EIGENVAL").exists() and (d / "EIGENVAL").stat().st_size > 0:
        nelect, kpts, weights, bands = read_eigenval(d / "EIGENVAL")

    # ---- (A) KPOINTS_OPT: 从 vasprun.xml 取路径本征值 ----
    if (d / "KPOINTS_OPT").exists():
        vr = d / "vasprun.xml"
        if not vr.exists():
            raise ValueError(
                "目录里有 KPOINTS_OPT，但没有 vasprun.xml。KPOINTS_OPT 的能带本征值"
                "只写在 vasprun.xml 里（VASP 不产出 EIGENVAL_OPT），"
                "请把计算目录的 vasprun.xml 一起带过来。")
        got = read_vasprun_kpoints_opt(vr)
        if got is None:
            raise ValueError(
                "vasprun.xml 里没有 <eigenvalues_kpoints_opt> 块——说明自洽还没收敛，"
                "KPOINTS_OPT 的 one-shot 根本没跑（作业可能被墙钟砍掉了）。")
        kopt, eopt = got
        eopt = np.array(eopt, dtype=float)
        if kopt is None or len(kopt) != len(eopt):
            kopt = read_kpoints_list(d / "KPOINTS_OPT")   # 退回直接读输入文件
        kopt = np.array(kopt, dtype=float)
        if nelect is None:
            raise ValueError("缺少 EIGENVAL，无法确定 NELECT")
        return nelect, kopt, eopt, len(kopt), "vasprun.xml (kpoints_opt)"

    # ---- (B) 零权重方案 ----
    if bands is None:
        raise ValueError("既没有 KPOINTS_OPT，也没有 EIGENVAL，无法提取能带路径")
    mask = weights == 0
    if mask.sum() > 0:
        return nelect, kpts[mask], bands[mask], len(kpts), "EIGENVAL(zero-weight)"

    # ---- 兜底：KPOINTS_OPT 没被拷进来，但 vasprun.xml 里其实有 kpoints_opt 块 ----
    vr = d / "vasprun.xml"
    if vr.exists():
        got = read_vasprun_kpoints_opt(vr)
        if got is not None:
            kopt, eopt = got
            return (nelect, np.array(kopt, dtype=float), np.array(eopt, dtype=float),
                    len(kopt), "vasprun.xml (kpoints_opt)")

    raise ValueError(
        "EIGENVAL 里没有零权重 k 点，也没有 KPOINTS_OPT / vasprun.xml 的 kpoints_opt 块——"
        "既不是『均匀网格+零权重路径』方案，也不是 KPOINTS_OPT 方案，无法提取能带路径")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="搭建 step3_band_plot 并提取 PBE/PBE+D3/PBEsol 能带、带隙、绘图"
    )
    ap.add_argument("--step3-dir", default=STEP3_DIR,
                    help=f"step3 目录名，默认 {STEP3_DIR}")
    ap.add_argument("--out-dir", default=OUT_DIR,
                    help=f"输出目录名，默认 {OUT_DIR}")
    ap.add_argument("--emin", type=float, default=-3.0, help="画图窗口下限(eV, 相对VBM)")
    ap.add_argument("--emax", type=float, default=4.0, help="画图窗口上限(eV, 相对VBM)")
    ap.add_argument("--labels", default=None,
                    help='手动高对称点: "G,0,0,0;M,0.5,0,0;..."（默认用六方 3D 内置表）')
    ap.add_argument("--prefix", default="band", help="输出文件名前缀，默认 band（done_marker 与下游 read_bandgap 都读 band_summary.json）")
    args = ap.parse_args()

    src = Path(args.step3_dir).resolve()
    dst = Path(args.out_dir).resolve()
    base = {"step": "step3_band_plot", "step3_dir": str(src),
            "out_dir": str(dst),
            "checked_at": datetime.now().isoformat(timespec="seconds")}

    if not src.is_dir():
        emit({**base, "status": "error",
              "reason": f"找不到 {src} —— 请在父目录下运行本脚本"}, 40)
    if not (src / "EIGENVAL").exists():
        emit({**base, "status": "error",
              "reason": f"缺少 {src / 'EIGENVAL'} —— step3 必须先跑完"}, 40)
    if (src / "KPOINTS_OPT").exists() and not (src / "vasprun.xml").exists():
        emit({**base, "status": "error",
              "reason": f"{src} 用了 KPOINTS_OPT 方案，但缺 vasprun.xml —— 路径本征值只写在里面"}, 40)
    for name in ("POSCAR",):
        if not (src / name).exists():
            emit({**base, "status": "error",
                  "reason": f"缺少 {src / name} —— step3 必须先跑完"}, 40)

    # 1) 搭建输出目录：拷贝输入（自包含，之后所有读写都在这里）
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in COPY_INPUTS:
        p = src / name
        if p.exists():
            shutil.copyfile(p, dst / name)
            copied.append(name)
    log(f"[..] 已拷贝输入到 {dst.name}/: {', '.join(copied)}")

    # E-fermi 只从 step3 OUTCAR 提取数值，不拷贝大文件
    efermi = read_efermi(src / "OUTCAR")

    # 2) 解析（从副本读，保证目录自包含）
    try:
        nelect, kpath, epath, nk_total, esrc = load_path_bands(dst)
        lat = read_lattice(dst / "POSCAR")
    except Exception as exc:
        emit({**base, "status": "error", "reason": f"解析失败: {exc}"}, 40)

    soc = (read_incar_flag(dst / "INCAR", "LSORBIT") or "").upper().startswith(".T")
    func = detect_func(dst / "workflow_method.txt", dst / "INCAR")
    func_disp = FUNC_PRETTY.get(func, func.upper())

    n_path = len(kpath)
    nbands = epath.shape[1]
    nspin = epath.shape[2] if epath.ndim == 3 else 1
    spin_txt = "ISPIN=2(↑/↓)" if nspin == 2 else ("SOC" if soc else "ISPIN=1")
    log(f"[..] 能带来源: {esrc}  路径点 {n_path} 个（该文件共 {nk_total} 个 k 点）")
    log(f"[..] 泛函={func_disp}  NELECT={nelect}  NBANDS={nbands}  {spin_txt}")

    # 占据带数：ISPIN=2 / SOC 每个自旋通道每带 1 电子；共线非磁每带 2 电子
    if nspin == 2:
        nocc_f = float(nelect)
    else:
        nocc_f = float(nelect) if soc else nelect / 2.0
    if abs(nocc_f - round(nocc_f)) > 1e-6:
        emit({**base, "status": "error",
              "reason": f"占据带数非整数({nocc_f})——金属或部分占据，请人工处理"}, 40)
    nocc = int(round(nocc_f))
    nstate = nbands * nspin
    if nocc + 1 > nstate:
        emit({**base, "status": "error",
              "reason": f"NBANDS×spin={nstate} 不足以包含 CBM（需要 >= {nocc + 1}）"}, 40)

    # 带隙：ISPIN=2 每个 k 点把上下自旋合并排序，跨通道取 VBM/CBM
    eflat = _flatten_spins(epath)
    vb, cb = eflat[:, nocc - 1], eflat[:, nocc]
    iv, ic = int(np.argmax(vb)), int(np.argmin(cb))
    evbm, ecbm = float(vb[iv]), float(cb[ic])
    gap = ecbm - evbm
    direct_gaps = cb - vb
    idg = int(np.argmin(direct_gaps))
    min_direct = float(direct_gaps[idg])
    same_k = np.allclose(kpath[iv], kpath[ic], atol=1e-6)

    # ---- 用自洽网格的本征值交叉检验：路径是否漏掉了真正的带边 ----
    mesh_info, mesh_warn = mesh_gap_check(dst / "EIGENVAL", nocc, evbm, ecbm)
    if mesh_warn:
        log("[warn] " + mesh_warn)
    elif mesh_info:
        log("[..] 自洽网格交叉检验通过：网格带隙 %.4f eV，与路径一致"
            % mesh_info["gap_mesh_eV"])

    if gap <= 0:
        log("[warn] 带隙 <= 0：体系为金属或半金属，能量零点改用 E-fermi")
        ezero, zero_name = (efermi if efermi is not None else evbm), "E-fermi"
    else:
        ezero, zero_name = evbm, "VBM"

    # 横轴与标签：优先用 step3 写出的 kpath.json（任何晶系通用、零猜测）
    axis = None
    kmeta = {}
    kjson = dst / "kpath.json"
    if kjson.exists() and not args.labels:
        try:
            kmeta = json.loads(kjson.read_text(encoding="utf-8"))
            axis = build_axis_from_kpath(kpath, lat, kmeta)
        except Exception as exc:
            log(f"[warn] 读 kpath.json 失败({exc})，退回坐标匹配模式")
    if axis is None:
        label_map = parse_manual_labels(args.labels) if args.labels else KPT_COORDS
        x, breaks, ticks = build_axis(kpath, lat, label_map)
        axis_src = "manual" if args.labels else "内置六方表(坐标匹配)"
    else:
        x, breaks, ticks = axis
        axis_src = "kpath.json (%s)" % kmeta.get("method", "?")
    log(f"[..] 横轴/标签来源: {axis_src}")

    # 3) 输出 band-dft-cpu.dat / klabels
    dat = dst / f"{args.prefix}.dat"
    with open(dat, "w") as f:
        f.write(f"# functional: {func_disp}{'+SOC' if soc else ''}"
                f"{' ISPIN=2' if nspin == 2 else ''}\n")
        f.write(f"# k-distance(1/Angst)  E-E_{zero_name}(eV) x {nbands} bands"
                f"{' x 2 spins' if nspin == 2 else ''}\n")
        for i in range(len(x)):
            if nspin == 2:
                vals = [e for s in range(2) for e in (epath[i, :, s] - ezero)]
            else:
                vals = [e for e in (epath[i] - ezero)]
            f.write("%12.6f " % x[i] +
                    " ".join("%10.4f" % v for v in vals) + "\n")

    klab = dst / f"{args.prefix}_klabels.txt"
    with open(klab, "w") as f:
        f.write("# label  k-distance\n")
        for xt, lab in ticks:
            f.write("%-6s %12.6f\n" % (lab, xt))

    # 4) 画图
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 5))
    if nspin == 2:
        spin_colors = ("#1f4e79", "#c0392b")   # 自旋上=蓝，自旋下=红
        for s in range(2):
            for b in range(nbands):
                ax.plot(x, epath[:, b, s] - ezero, lw=1.0, color=spin_colors[s],
                        label=("spin up" if s == 0 else "spin down") if b == 0 else None)
        ax.legend(fontsize=8, loc="best")
    else:
        for b in range(nbands):
            ax.plot(x, epath[:, b] - ezero, lw=1.0, color="#1f4e79")
    for xb in breaks:
        ax.axvline(xb, color="k", lw=0.8)
    for xt, _ in ticks:
        ax.axvline(xt, color="0.75", lw=0.6, zorder=0)
    ax.axhline(0.0, color="0.4", lw=0.8, ls="--")
    ax.set_xticks([t[0] for t in ticks])
    ax.set_xticklabels([t[1] for t in ticks])
    ax.set_xlim(x.min(), x.max())
    ax.set_ylim(args.emin, args.emax)
    ax.set_ylabel(f"$E - E_{{\\rm {zero_name}}}$ (eV)")
    title = "%s   %s%s" % (Path(args.step3_dir).name, func_disp, "+SOC" if soc else "")
    if gap > 0:
        title += "   $E_g$=%.3f eV (%s)" % (gap, "direct" if same_k else "indirect")
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    png = dst / f"{args.prefix}.png"
    fig.savefig(png, dpi=300)
    log(f"[OK] {dat.name} / {klab.name} / {png.name}")

    def kfmt(v):
        return [round(float(t), 6) for t in v]

    result = {**base, "status": "ok",
              "functional": func, "functional_display": func_disp,
              "soc": soc, "nspin": nspin, "nelect": nelect, "n_path_kpts": int(n_path),
              "band_source": esrc,
              "gap_eV": round(gap, 4),
              "gap_type": ("metal" if gap <= 0 else ("direct" if same_k else "indirect")),
              "min_direct_gap_eV": round(min_direct, 4),
              "vbm": {"E_eV": round(evbm, 4), "k_frac": kfmt(kpath[iv])},
              "cbm": {"E_eV": round(ecbm, 4), "k_frac": kfmt(kpath[ic])},
              "efermi_scf_eV": efermi,
              "mesh_cross_check": mesh_info,
              "mesh_cross_check_warning": mesh_warn,
              "energy_zero": zero_name,
              "inputs_copied": copied,
              "files": {"dat": str(dat), "klabels": str(klab), "png": str(png),
                        "summary": str(dst / f"{args.prefix}_summary.json")},
              "labels_found": [t[1] for t in ticks],
              "kpath_source": axis_src,
              "kpath_method": kmeta.get("method"),
              "kpath_note": kmeta.get("note")}

    # 5) 摘要落盘
    (dst / f"{args.prefix}_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    emit(result, 0)


if __name__ == "__main__":
    main()