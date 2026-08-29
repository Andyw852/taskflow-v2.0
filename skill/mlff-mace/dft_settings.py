#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dft_settings.py —— step5_label 的 DFT 设置推导（纯标准库，登录节点/venv 都能跑）。

从 step1_relax 的输出推导单点计算的全部设置：
    - ENCUT   = ceil(1.5 × max ENMAX)（从 POTCAR；ENCUT_OVERRIDE 可覆盖）
    - ISMEAR/SIGMA = 由 step1 EIGENVAL 带隙决定：gap > 0.1 eV → 0/0.05，否则 1/0.2
    - ISPIN/MAGMOM = 由 step1 OUTCAR 末次磁矩决定：max|m| > 0.1 μB → ISPIN=2 并继承
    - LMAXMIX      = 含 f 元素 6 / 含 d 元素 4 / 否则 2
    - 指纹 dft_fingerprint()：ENCUT/PREC/GGA/IVDW/ISMEAR/SIGMA/ISPIN/LREAL/LASPH/
      LMAXMIX/EDIFF/LDAU 设置 + POTCAR TITEL 行；k 点密度单独比较（KSPACING_TOL）
"""
import math
import re
import sys
from pathlib import Path

D_ELEMS = {
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "La", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Ga", "Ge", "In", "Sn", "Tl", "Pb", "Bi",
}
F_ELEMS = {
    "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er",
    "Tm", "Yb", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm",
}
# 孤立原子的高自旋初始磁矩（μB）——只在体系磁性、且该元素在晶体中平均磁矩 < 0.1 时用
ISO_HIGH_SPIN = {
    "Sc": 1.0, "Ti": 1.0, "V": 3.0, "Cr": 4.0, "Mn": 5.0, "Fe": 4.0,
    "Co": 3.0, "Ni": 2.0, "Cu": 1.0, "Ce": 1.0, "Pr": 2.0, "Nd": 3.0,
    "Pm": 4.0, "Sm": 5.0, "Eu": 7.0, "Gd": 7.0, "Tb": 6.0, "Dy": 5.0,
    "Ho": 4.0, "Er": 3.0, "Tm": 2.0, "Yb": 1.0, "U": 2.0, "Np": 3.0, "Pu": 4.0,
}


def read_incar(path):
    """INCAR → {KEY: value}（去注释、按分号拆）。"""
    vals = {}
    p = Path(path)
    if not p.is_file():
        return vals
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "!")):
            continue
        text = text.split("#", 1)[0].split("!", 1)[0].strip()
        for part in text.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                vals[k.strip().upper()] = v.strip()
    return vals


def read_poscar(path):
    """POSCAR → (symbols, counts, lattice, frac)。复用 dim_common 的解析。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dim_common import read_poscar_cell_frac
    lat, frac = read_poscar_cell_frac(str(path))
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    idx = 5
    tokens = lines[idx].split()
    if not re.fullmatch(r"[+-]?\d+", tokens[0]):
        idx += 1
    counts = [int(x) for x in lines[idx].split()]
    symbols = tokens if (tokens and not re.fullmatch(r"[+-]?\d+", tokens[0])) else None
    return symbols, counts, lat, frac


def encut_from_potcar(potcar, factor=1.5, override=None):
    """-> (encut, note)。override 是 int/str 或 None。"""
    if override not in (None, ""):
        try:
            return int(float(override)), "ENCUT_OVERRIDE"
        except ValueError:
            sys.exit("[ERROR] ENCUT_OVERRIDE=%r 不是数字" % override)
    p = Path(potcar)
    if not p.is_file():
        sys.exit("[ERROR] 找不到 POTCAR：%s（step1_relax 的 gen_need 里漏了？）" % p)
    vals = []
    for line in p.read_text(errors="ignore").splitlines():
        m = re.search(r"ENMAX\s*=\s*([\d.]+)", line)
        if m:
            vals.append(float(m.group(1)))
    if not vals:
        sys.exit("[ERROR] %s 里没有 ENMAX" % p)
    encut = int(math.ceil(factor * max(vals) / 10.0)) * 10
    return encut, "ceil(%.1f x %.1f)" % (factor, max(vals))


def read_bandgap(eigenval_path):
    """EIGENVAL → (gap_eV, note)。金属（最高占据 > 最低空带）时 gap 为负。
    VASP 各版本的 k 点头/带行/空行分布不完全一致，这里按结构扫描：
    4 个浮点数的行 = k 点头；其后连续 NBANDS 个非空行 = 带（带号, E, occ, [E2, occ2]）。"""
    p = Path(eigenval_path)
    if not p.is_file():
        return None, "EIGENVAL 缺失"
    lines = p.read_text(errors="ignore").splitlines()
    if len(lines) < 8:
        return None, "EIGENVAL 行数不足"
    nk = nb = None
    for ln in lines[5:9]:
        t = ln.split()
        if len(t) >= 3 and all(_is_num(x) for x in t[:3]):
            nk, nb = int(t[-2]), int(t[-1])
            break
    if nk is None or nb is None:
        return None, "EIGENVAL 头解析失败"
    e_occ, e_unocc = [], []
    i, n_read = 6, 0
    while i < len(lines) and n_read < nk:
        t = lines[i].split()
        if len(t) == 4 and all(_is_num(x) for x in t):
            n_read += 1
            i += 1
            bands = 0
            while i < len(lines) and bands < nb:
                b = lines[i].split()
                i += 1
                if not b:
                    continue
                bands += 1
                try:
                    e, occ = float(b[1]), float(b[2])
                except (ValueError, IndexError):
                    continue
                if len(b) >= 5:          # 自旋极化：取两个通道的最小占据能/最大空带能
                    e2, occ2 = float(b[3]), float(b[4])
                    if occ >= 0.5 or occ2 >= 0.5:
                        e_occ.append(min(e, e2))
                    if occ < 0.5 or occ2 < 0.5:
                        e_unocc.append(max(e, e2))
                else:
                    (e_occ if occ >= 0.5 else e_unocc).append(e)
            continue
        i += 1
    if not e_occ or not e_unocc:
        return None, "EIGENVAL 无法区分占据/空带（0 带隙读不出）"
    return min(e_unocc) - max(e_occ), "EIGENVAL"


def _is_num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def decide_ismear(gap, gap_note):
    """(gap>0.1 eV) → (ISMEAR=0, SIGMA=0.05)；否则 (1, 0.2)。"""
    if gap is None:
        print("[WARN] 带隙读不出（%s），按金属处理：ISMEAR=1 SIGMA=0.2" % gap_note)
        return "1", "0.2", "带隙读不出（%s），按金属" % gap_note
    if gap > 0.1:
        return "0", "0.05", "带隙 %.3f eV > 0.1" % gap
    return "1", "0.2", "带隙 %.3f eV ≤ 0.1，按金属" % gap


def read_magmom(outcar_path):
    """OUTCAR 末次 per-ion 磁矩 → (moments列表, note)。非磁性返回 ([], note)。"""
    p = Path(outcar_path)
    if not p.is_file():
        return None, "OUTCAR 缺失"
    text = p.read_text(errors="ignore")
    blocks = re.findall(r"magnetization \(x\)\s*\n\s*-+\s*\n((?:\s*\d+\s+[-+0-9.]+.*\n)+)",
                        text)
    if not blocks:
        if re.search(r"\bISPIN\s*=\s*2", text):
            return [], "OUTCAR 里有 ISPIN=2 但找不到 magnetization (x) 块"
        return [], "非磁性（无 magnetization (x) 块）"
    moms = []
    for line in blocks[-1].splitlines():
        t = line.split()
        if len(t) >= 5:
            try:
                moms.append(float(t[4]))       # s p d tot 的第 5 列 = 总磁矩
            except ValueError:
                continue
    if not moms:
        return [], "magnetization (x) 块为空"
    return moms, "末次 %d 离子磁矩" % len(moms)


def magnetic_setting(moments, tol=0.1):
    """-> (ispin, note)。max|m| > tol → 磁性。"""
    if moments is None:
        return "1", "磁矩读不出，按非磁"
    if not moments:
        return "1", "末次磁矩全 0（非磁）"
    mmax = max(abs(x) for x in moments)
    if mmax > tol:
        return "2", "max|m| = %.2f μB > %.2f" % (mmax, tol)
    return "1", "max|m| = %.2f μB ≤ %.2f，按非磁" % (mmax, tol)


def decide_lmaxmix(symbols):
    syms = set(symbols or [])
    if syms & F_ELEMS:
        return 6, "含 f 元素 %s" % "/".join(sorted(syms & F_ELEMS))
    if syms & D_ELEMS:
        return 4, "含 d 元素 %s" % "/".join(sorted(syms & D_ELEMS))
    return 2, "无 d/f 元素"


def potcar_titels(potcar):
    out = []
    for line in Path(potcar).read_text(errors="ignore").splitlines():
        if "TITEL" in line:
            out.append(line.split("=", 1)[1].strip())
    return out


def kspacing_density(kpoints_path, lattice):
    """等效 k 点密度 KSPACING = 2π / min_i(N_i·|b_i|)，N_i 从 KPOINTS 自动网格行读。
    Γ-only（1 1 1）→ N_i=1。-> (kspacing, note)"""
    try:
        import dim_common
        _inv = dim_common._inv3
        _norm = dim_common._norm
    except Exception:
        def _inv(m):
            return [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        def _norm(v):
            return (sum(x * x for x in v)) ** 0.5
    p = Path(kpoints_path)
    nums = [1, 1, 1]
    if p.is_file():
        lines = p.read_text(errors="ignore").splitlines()
        if len(lines) >= 4 and lines[1].strip().startswith("0"):
            try:
                nums = [int(x) for x in lines[3].split()[:3]]
            except (ValueError, IndexError):
                pass
    inv = _inv(lattice)
    rec_len = [_norm([inv[i][j] for j in range(3)]) for i in range(3)]  # 2π 稍后乘
    blen = [rec_len[i] * nums[i] for i in range(3)]
    import math as _m
    return 2.0 * _m.pi / max(min(blen), 1e-12), "网格 %d %d %d" % tuple(nums)


def dft_fingerprint(incar_vals, potcar, lattice, kpoints, func):
    """-> (fingerprint_str, kspacing_float)。k 点密度单独返回，比较用容差。"""
    keys = ["PREC", "ENCUT", "GGA", "IVDW", "ISMEAR", "SIGMA", "ISPIN",
            "LREAL", "LASPH", "LMAXMIX", "EDIFF", "LDAU", "LDAUTYPE",
            "LDAUL", "LDAUU", "LDAUJ"]
    parts = ["FUNC=%s" % func]
    for k in keys:
        v = incar_vals.get(k, "-")
        parts.append("%s=%s" % (k, (v.split()[0] if v else "-")))
    parts.append("POTCAR=%s" % ",".join(potcar_titels(potcar)))
    ks, note = kspacing_density(kpoints, lattice)
    return "|".join(parts), ks


def check_fingerprint(fp_a, ks_a, fp_b, ks_b, kspacing_tol=0.20):
    """比较两份指纹。返回 (ok, 诊断文本)。k 密度超容差 WARN 不阻断；其余不一致 FAIL。"""
    if fp_a == fp_b:
        msg = "指纹一致"
        if abs(ks_a - ks_b) / max(min(ks_a, ks_b), 1e-12) > kspacing_tol:
            msg += "；k 点密度 %.3f vs %.3f 1/Å 超容差 %.0f%%（WARN，不阻断）" % (
                ks_a, ks_b, kspacing_tol * 100)
        return True, msg
    pa, pb = dict(p.split("=", 1) for p in fp_a.split("|") if "=" in p), \
             dict(p.split("=", 1) for p in fp_b.split("|") if "=" in p)
    diffs = [k for k in pa if pb.get(k) != pa[k]]
    diffs += [k for k in pb if k not in pa]
    return False, "指纹不一致，键 %s：现有 %s vs 新 %s" % (
        ",".join(sorted(diffs)),
        {k: pa.get(k) for k in diffs}, {k: pb.get(k) for k in diffs})
