#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step4.1_plot_band.py
========================
在父目录（含 step4_HSE_band/）下运行，搭建并完成 step4_band_plot：
提取 HSE 能带、计算带隙、绘图。与 gen_step1~4 相同的目录组织方式。

做的事：
    1. 新建 step4_band_plot/，把 step4_HSE_band 的输入拷贝进来（自包含、可追溯）：
           EIGENVAL, POSCAR, INCAR, KPOINTS, workflow_method.txt
       （OUTCAR 太大不拷，只提取 E-fermi 记入摘要。）
    2. 读 EIGENVAL：按 k 点权重自动分离“均匀网格点(权重>0)”与
       “能带路径点(权重=0)”，只用后者画能带（HSE 零权重方案）。
    3. 读 POSCAR 晶格 -> 倒格子，计算路径点的累积 k 距离作为横轴；
       段与段之间的跳变（如 A|L、K|H）自动识别为断点，不计入距离。
    4. 高对称点标签：按六方 3D 高对称点分数坐标匹配（与 gen_step3 一致），
       非六方体系可用 --labels 手动指定。
    5. 带隙：SOC 下每条带占 1 个电子（NELECT 条占据带）；
       VBM = 第 NELECT 条带最大值，CBM = 第 NELECT+1 条带最小值；
       同 k 点 -> 直接带隙，否则间接（同时报告最小直接带隙）。
    6. 输出（全部写入 step4_band_plot/）：
           band-dft-cpu.dat            k距离 + 各带能量（已以 VBM 为零点）
           band-dft-cpu.png            能带图（标题含带隙类型/数值；间接时附最小直接带隙）
           band_klabels.txt    高对称点位置
           band_summary.json   带隙/VBM/CBM 摘要（与 stdout JSON 相同）

agent 约定（与检查脚本一致）：
    stdout 只输出一行 JSON；stderr 是过程日志；
    退出码 0=成功  40=错误（缺文件/解析失败等）。

切片拼接（gen_step4_HSE.py --kpath-slice 生成的多段目录）：
    --step4-dir 支持逗号分隔或通配符；给出多个目录时按各自 kpath.json 的
    "slice" 字段（offset/count/total）排序拼接成整条能带。默认目录
    step4_HSE_band 不存在但存在 step4_HSE_band_p*of* 时自动进入拼接模式。
    拼接模式直接从各段目录流式读 vasprun.xml（不拷贝大文件）。

用法：
    cd <父目录>      # 里面有 step4_HSE_band/（已跑完）
    python gen_step4.1_plot_band.py
    python gen_step4.1_plot_band.py --step4-dir "step4_HSE_band_p*of2"
    python gen_step4.1_plot_band.py --emin -2 --emax 3
    python gen_step4.1_plot_band.py --labels "G,0,0,0;M,0.5,0,0;..."
    python gen_step4.1_plot_band.py --no-mark-extrema     # 不在图上圈 VBM/CBM

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
STEP4_DIR = "step2_bandgap/step2.3_hse"      # 源目录
OUT_DIR   = "step2_bandgap/step2.3_hse_plot"     # 目标目录（输入输出都在这里）

# 从 step4_HSE_band 拷贝到 step4_band_plot 的输入（存在才拷；OUTCAR 太大只提取 E-fermi）
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
JUMP_FACTOR = 3.0     # 相邻路径点间距超过中位数的该倍数 -> 判定为段间跳变

MARK_COLOR = "#c00000"   # VBM/CBM 标记颜色
BAND_COLOR = "#1f4e79"   # 能带线颜色
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
      bands  : (nk, nbands) 能量 eV
    兼容 ISPIN=1（含 SOC 非共线）。ISPIN=2 会报错提示。
    """
    lines = path.read_text(errors="ignore").splitlines()
    ispin = int(lines[0].split()[3])
    if ispin != 1:
        raise ValueError("本脚本只支持 ISPIN=1（含 SOC）；检测到 ISPIN=2")
    nelect, nk, nb = (int(float(x)) for x in lines[5].split()[:3])

    kpts, weights, bands = [], [], []
    i = 6
    for _ in range(nk):
        while i < len(lines) and not lines[i].strip():
            i += 1
        t = lines[i].split()
        kpts.append([float(t[0]), float(t[1]), float(t[2])])
        weights.append(float(t[3]))
        i += 1
        eb = []
        for _ in range(nb):
            eb.append(float(lines[i].split()[1]))
            i += 1
        bands.append(eb)
    return nelect, np.array(kpts), np.array(weights), np.array(bands)


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
    if mask.sum() == 0 or bands.shape[1] < nocc + 1:
        return None, None
    mvb, mcb = bands[mask][:, nocc - 1], bands[mask][:, nocc]
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
    keep = 0          # >0 表示当前在“需要保留的子树”内部，不能 clear
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
                            bands = [[float(r.text.split()[0]) for r in ks.findall("r")]
                                     for ks in spins[0].findall("set")]
                elem.clear()
            elif keep == 0:
                elem.clear()          # 丢掉不关心的部分，控制内存
    except ET.ParseError as exc:
        raise ValueError(f"vasprun.xml 解析失败（作业可能被中途杀掉）: {exc}")

    if bands is None:
        return None
    return kpts, bands


def expand_step4_dirs(spec: str):
    """--step4-dir 支持 'a,b,c' / 通配符；单个不存在的目录自动找 _p*of* 兄弟目录。"""
    import glob as _glob
    cand = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if any(ch in tok for ch in "*?["):
            hit = sorted(_glob.glob(tok))
            if not hit:
                raise ValueError(f"通配符 {tok!r} 没匹配到任何目录")
            cand += hit
        else:
            cand.append(tok)
    if len(cand) == 1 and not Path(cand[0]).is_dir():
        sib = sorted(_glob.glob(cand[0] + "_p*of*"))
        if sib:
            log(f"[..] {cand[0]} 不存在，自动改用切片目录: {', '.join(sib)}")
            cand = sib
    seen, out = set(), []
    for c in cand:
        p = Path(c).resolve()
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def slice_kmeta(kmeta: dict, n_have: int):
    """单段目录：kpath.json 存的是完整路径 + slice 字段 -> 裁出本段元数据。"""
    sl = kmeta.get("slice")
    if not sl:
        return kmeta
    o, c = int(sl.get("offset", 0)), int(sl.get("count", 0))
    if c != n_have or len(kmeta.get("kpoints") or []) != int(sl.get("total", -1)):
        log("[warn] kpath.json 的 slice 字段与本征值数量对不上，忽略之")
        return kmeta
    out = dict(kmeta)
    out["kpoints"]      = kmeta["kpoints"][o:o + c]
    out["point_labels"] = (kmeta.get("point_labels") or [])[o:o + c]
    out["breaks"]       = [b - o for b in kmeta.get("breaks", []) if o < b < o + c]
    return out


def load_merged_parts(dirs):
    """
    多段 KPOINTS_OPT 目录 -> 按 slice.offset 排序拼接。
    返回 (kpath, epath, kmeta_full, sources)；NELECT 另从首段 EIGENVAL 读。
    """
    parts = []
    total = None
    for d in dirs:
        kj = d / "kpath.json"
        meta = json.loads(kj.read_text(encoding="utf-8")) if kj.exists() else {}
        sl = meta.get("slice") or {}
        vr = d / "vasprun.xml"
        if not vr.exists():
            raise ValueError(f"{d} 缺 vasprun.xml —— 该段还没跑完")
        got = read_vasprun_kpoints_opt(vr)
        if got is None:
            raise ValueError(f"{d}/vasprun.xml 里没有 <eigenvalues_kpoints_opt> 块 —— "
                             "该段自洽没收敛或 one-shot 没跑完")
        kopt, eopt = got
        eopt = np.array(eopt, dtype=float)
        if kopt is None or len(kopt) != len(eopt):
            kopt = read_kpoints_list(d / "KPOINTS_OPT")
        kopt = np.array(kopt, dtype=float)
        off = sl.get("offset")
        cnt = sl.get("count")
        if cnt is not None and int(cnt) != len(kopt):
            raise ValueError(f"{d}: slice 声明 {cnt} 点，vasprun 实际 {len(kopt)} 点")
        if sl.get("total") is not None:
            t = int(sl["total"])
            if total is None:
                total = t
            elif total != t:
                raise ValueError(f"{d}: slice.total={t} 与其它段({total})不一致 —— 混了不同批次?")
        parts.append({"dir": d, "off": off, "k": kopt, "e": eopt, "meta": meta})

    if all(p["off"] is not None for p in parts):
        parts.sort(key=lambda p: p["off"])
        pos = 0
        for p in parts:
            if int(p["off"]) != pos:
                raise ValueError(
                    f"{p['dir'].name}: offset={p['off']} 但前面各段累计 {pos} 点 —— "
                    "段不连续（缺段或重复），请核对目录列表")
            pos += len(p["k"])
        if total is not None and pos != total:
            raise ValueError(f"各段共 {pos} 点，但完整路径应有 {total} 点 —— 少段了")
    else:
        log("[warn] 有目录缺 slice 元数据，按给定顺序直接拼接（自担风险）")

    nb = {p["e"].shape[1] for p in parts}
    if len(nb) != 1:
        raise ValueError(f"各段 NBANDS 不一致: {sorted(nb)} —— 不能拼接")

    kpath = np.concatenate([p["k"] for p in parts])
    epath = np.concatenate([p["e"] for p in parts])
    kmeta_full = dict(parts[0]["meta"])
    kmeta_full.pop("slice", None)
    return kpath, epath, kmeta_full, [p["dir"].name for p in parts]


def load_path_bands(d: Path):
    """
    读出“能带路径上的”本征值，自动兼容两种方案：

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
# 标题：带隙类型 + 数值（间接时附最小直接带隙）
# ---------------------------------------------------------------------------
def make_title(name, gap, same_k, min_direct):
    """
    金属   -> "<name>   metal / semimetal (no gap)"
    直接   -> "<name>   Eg = 1.234 eV (direct)"
    间接   -> "<name>   Eg = 1.234 eV (indirect);  Eg_dir = 1.567 eV"
    """
    if gap <= 0:
        return f"{name}   metal / semimetal (no gap)"
    if same_k:
        return "%s   $E_g$ = %.3f eV (direct)" % (name, gap)
    return ("%s   $E_g$ = %.3f eV (indirect);  $E_g^{\\rm dir}$ = %.3f eV"
            % (name, gap, min_direct))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="搭建 step4_band_plot 并提取 HSE 能带/带隙/绘图（供多智能体调用）"
    )
    ap.add_argument("--step4-dir", default=STEP4_DIR,
                    help=f"step4 HSE 计算目录名，默认 {STEP4_DIR}")
    ap.add_argument("--out-dir", default=OUT_DIR,
                    help=f"输出目录名，默认 {OUT_DIR}")
    ap.add_argument("--emin", type=float, default=-3.0, help="画图窗口下限(eV, 相对VBM)")
    ap.add_argument("--emax", type=float, default=4.0, help="画图窗口上限(eV, 相对VBM)")
    ap.add_argument("--labels", default=None,
                    help='手动高对称点: "G,0,0,0;M,0.5,0,0;..."（默认用六方 3D 内置表）')
    ap.add_argument("--prefix", default="band-dft-cpu", help="输出文件名前缀，默认 band-dft-cpu")
    ap.add_argument("--title", default=None,
                    help="自定义标题（默认用 step4 目录名 + 带隙信息）")
    ap.add_argument("--no-mark-extrema", action="store_true",
                    help="不在图上圈出 VBM/CBM（默认圈出）")
    args = ap.parse_args()

    try:
        dirs = expand_step4_dirs(args.step4_dir)
    except ValueError as exc:
        print(json.dumps({"status": "error", "reason": str(exc)},
                         ensure_ascii=False), flush=True)
        sys.exit(40)
    merge = len(dirs) > 1
    step4 = dirs[0]
    out = Path(args.out_dir).resolve()
    base = {"step": "step4_band_plot",
            "step4_dir": ",".join(str(d) for d in dirs),
            "merged_parts": len(dirs) if merge else None,
            "out_dir": str(out),
            "checked_at": datetime.now().isoformat(timespec="seconds")}

    for d in dirs:
        if not d.is_dir():
            emit({**base, "status": "error",
                  "reason": f"找不到 {d} —— 请在父目录下运行本脚本"}, 40)
    if not (step4 / "EIGENVAL").exists():
        emit({**base, "status": "error",
              "reason": f"缺少 {step4 / 'EIGENVAL'} —— step4 必须先跑完"}, 40)
    if not merge and (step4 / "KPOINTS_OPT").exists() and not (step4 / "vasprun.xml").exists():
        emit({**base, "status": "error",
              "reason": f"{step4} 用了 KPOINTS_OPT 方案，但缺 vasprun.xml —— 路径本征值只写在里面"}, 40)
    for name in ("POSCAR",):
        if not (step4 / name).exists():
            emit({**base, "status": "error",
                  "reason": f"缺少 {step4 / name} —— step4 必须先跑完"}, 40)

    # 1) 搭建输出目录：拷贝输入（自包含，之后所有读写都在这里）
    out.mkdir(parents=True, exist_ok=True)   # ke-dft-cpu：OUT_DIR 是嵌套路径
    copied = []
    copy_names = (["EIGENVAL", "POSCAR", "INCAR", "KPOINTS", "workflow_method.txt"]
                  if merge else COPY_INPUTS)   # 拼接模式不拷各段的大 vasprun/部分 KPOINTS_OPT
    for name in copy_names:
        src = step4 / name
        if src.exists():
            shutil.copyfile(src, out / name)
            copied.append(name)
    log(f"[..] 已拷贝输入到 {out.name}/: {', '.join(copied)}"
        + ("  （来自首段 %s；EIGENVAL/SCF 各段相同）" % step4.name if merge else ""))

    # E-fermi 只从 OUTCAR 提取数值，不拷贝大文件（各段同一 SCF，取第一个有的）
    efermi = None
    for d in dirs:
        efermi = read_efermi(d / "OUTCAR")
        if efermi is not None:
            break

    # 2) 解析
    try:
        if merge:
            kpath, epath, kmeta_full, part_names = load_merged_parts(dirs)
            (out / "kpath.json").write_text(
                json.dumps(kmeta_full, ensure_ascii=False, indent=1), encoding="utf-8")
            copied.append("kpath.json(merged)")
            nelect = read_eigenval(out / "EIGENVAL")[0]
            nk_total = len(kpath)
            esrc = "merged vasprun.xml (kpoints_opt) × %d: %s" % (
                len(part_names), "+".join(part_names))
        else:
            nelect, kpath, epath, nk_total, esrc = load_path_bands(out)
        lat = read_lattice(out / "POSCAR")
    except Exception as exc:
        emit({**base, "status": "error", "reason": f"解析失败: {exc}"}, 40)

    soc = (read_incar_flag(out / "INCAR", "LSORBIT") or "").upper().startswith(".T")

    n_path = len(kpath)
    nbands = epath.shape[1]
    log(f"[..] 能带来源: {esrc}  路径点 {n_path} 个（该文件共 {nk_total} 个 k 点）")
    log(f"[..] NELECT={nelect}  NBANDS={nbands}  SOC={'on' if soc else 'off'}")

    # 占据带数：SOC 每带 1 电子；共线非磁每带 2 电子
    nocc_f = float(nelect) if soc else nelect / 2.0
    if abs(nocc_f - round(nocc_f)) > 1e-6:
        emit({**base, "status": "error",
              "reason": f"占据带数非整数({nocc_f})——金属或部分占据，请人工处理"}, 40)
    nocc = int(round(nocc_f))
    if nocc + 1 > nbands:
        emit({**base, "status": "error",
              "reason": f"NBANDS={nbands} 不足以包含 CBM（需要 >= {nocc + 1}）"}, 40)

    # 带隙（只在路径点上取，与能带图一致）
    vb, cb = epath[:, nocc - 1], epath[:, nocc]
    iv, ic = int(np.argmax(vb)), int(np.argmin(cb))
    evbm, ecbm = float(vb[iv]), float(cb[ic])
    gap = ecbm - evbm
    direct_gaps = cb - vb
    idg = int(np.argmin(direct_gaps))
    min_direct = float(direct_gaps[idg])
    same_k = np.allclose(kpath[iv], kpath[ic], atol=1e-6)

    # ---- 用自洽网格的本征值交叉检验：路径是否漏掉了真正的带边 ----
    mesh_info, mesh_warn = mesh_gap_check(out / "EIGENVAL", nocc, evbm, ecbm)
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

    gap_type = "metal" if gap <= 0 else ("direct" if same_k else "indirect")
    if gap > 0:
        log("[..] E_g = %.4f eV (%s);  最小直接带隙 = %.4f eV @ k=%s"
            % (gap, gap_type, min_direct,
               np.round(kpath[idg], 4).tolist()))
    else:
        log("[..] 无带隙（metal/semimetal）")

    # 横轴与标签：优先用 step3 写出的 kpath.json（任何晶系通用、零猜测）
    axis = None
    kmeta = {}
    kjson = out / "kpath.json"
    if kjson.exists() and not args.labels:
        try:
            kmeta = json.loads(kjson.read_text(encoding="utf-8"))
            kmeta = slice_kmeta(kmeta, len(kpath))   # 单段目录: 完整元数据裁到本段
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

    # 3) 输出 band-dft-cpu.dat / klabels（全部写入输出目录）
    dat = out / f"{args.prefix}.dat"
    with open(dat, "w") as f:
        f.write(f"# k-distance(1/Angst)  E-E_{zero_name}(eV) x {epath.shape[1]} bands\n")
        for i in range(len(x)):
            f.write("%12.6f " % x[i] +
                    " ".join("%10.4f" % (e - ezero) for e in epath[i]) + "\n")

    klab = out / f"{args.prefix}_klabels.txt"
    with open(klab, "w") as f:
        f.write("# label  k-distance\n")
        for xt, lab in ticks:
            f.write("%-6s %12.6f\n" % (lab, xt))

    # 4) 画图
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 5))
    for b in range(epath.shape[1]):
        ax.plot(x, epath[:, b] - ezero, lw=1.0, color=BAND_COLOR)
    for xb in breaks:
        ax.axvline(xb, color="k", lw=0.8)
    for xt, _ in ticks:
        ax.axvline(xt, color="0.75", lw=0.6, zorder=0)
    ax.axhline(0.0, color="0.4", lw=0.8, ls="--")

    # VBM / CBM 标记（能量已减 ezero；有带隙时 ezero==VBM，故 VBM 在 0、CBM 在 gap）
    marked = False
    if gap > 0 and not args.no_mark_extrema:
        ax.plot(x[iv], evbm - ezero, "o", ms=5, mfc="none",
                mec=MARK_COLOR, mew=1.2, zorder=5, label="VBM")
        ax.plot(x[ic], ecbm - ezero, "s", ms=5, mfc="none",
                mec=MARK_COLOR, mew=1.2, zorder=5, label="CBM")
        ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
        marked = True

    ax.set_xticks([t[0] for t in ticks])
    ax.set_xticklabels([t[1] for t in ticks])
    ax.set_xlim(x.min(), x.max())
    ax.set_ylim(args.emin, args.emax)
    ax.set_ylabel(f"$E - E_{{\\rm {zero_name}}}$ (eV)")

    disp_name = re.sub(r"_p\d+of\d+$", "", step4.name)
    if merge:
        disp_name += " (merged %d parts)" % len(dirs)
    title = args.title or make_title(disp_name, gap, same_k, min_direct)
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    png = out / f"{args.prefix}.png"
    fig.savefig(png, dpi=300)
    log(f"[OK] {dat.name} / {klab.name} / {png.name}")

    def kfmt(v):
        return [round(float(t), 6) for t in v]

    result = {**base, "status": "ok",
              "soc": soc, "nelect": nelect, "n_path_kpts": int(n_path),
              "band_source": esrc,
              "gap_eV": round(gap, 4),
              "gap_type": gap_type,
              "min_direct_gap_eV": round(min_direct, 4),
              "min_direct_gap_k_frac": kfmt(kpath[idg]),
              "vbm": {"E_eV": round(evbm, 4), "k_frac": kfmt(kpath[iv])},
              "cbm": {"E_eV": round(ecbm, 4), "k_frac": kfmt(kpath[ic])},
              "efermi_scf_eV": efermi,
              "mesh_cross_check": mesh_info,
              "mesh_cross_check_warning": mesh_warn,
              "energy_zero": zero_name,
              "plot_title": title,
              "extrema_marked": marked,
              "inputs_copied": copied,
              "files": {"dat": str(dat), "klabels": str(klab), "png": str(png),
                        "summary": str(out / f"{args.prefix}_summary.json")},
              "labels_found": [t[1] for t in ticks],
              "kpath_source": axis_src,
              "kpath_method": kmeta.get("method"),
              "kpath_note": kmeta.get("note")}

    # 5) 摘要同时落盘，方便 agent/人工事后查阅
    (out / f"{args.prefix}_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    emit(result, 0)


if __name__ == "__main__":
    main()