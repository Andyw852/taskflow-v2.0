# -*- coding: utf-8 -*-
"""
cheap_features.py —— 纯 numpy / 标准库 实现"易算特征"提取。

只依赖 POSCAR 文本 + 元素属性表(element_properties.json)，不做任何 DFT。
输出的 14 个特征与训练替代模型时的特征顺序严格一致(见 FEATURE_ORDER)。

本模块被两处复用：
  1. matexplore 软件包(hypothesis.cheap_features)
  2. taskflow 技能 skill/te-screen/(作为 gen_need 依赖文件捆绑)
故必须保持"只用标准库 + numpy"，不 import pymatgen / spglib / scipy。
"""
import json
import numpy as np

# 与 scripts/train_surrogate.py 的 CHEAP 列表顺序一一对应
FEATURE_ORDER = [
    "electronegativity_mean", "electronegativity_range",
    "atomic_mass_mean", "atomic_mass_max", "atomic_radius_mean",
    "Z_mean", "ionization_energy_mean", "electron_affinity_mean",
    "row_mean", "group_range",
    "density", "nsites", "inplane_area", "aspect_c_over_sqrtA",
]

# magpie 统计所用的 8 个元素属性(与 jarvis atlas 脚本 37 的 PROP_NAMES 一致)
PROP_NAMES = ["electronegativity", "atomic_mass", "atomic_radius",
              "row", "group", "ionization_energy", "electron_affinity", "Z"]


def load_element_properties(path):
    with open(path, "r") as f:
        return json.load(f)


def parse_poscar(text):
    """极简 POSCAR/VASP5 解析。返回 dict(lattice=3x3, species=[...], natoms)。"""
    lines = [ln for ln in text.splitlines()]
    # 跳过可能的标题行(VASP5 第一行是注释，第二行是缩放因子)
    i = 0
    comment = lines[i].strip()
    i += 1
    scale = 1.0
    try:
        scale = float(lines[i].split()[0])
        i += 1
    except (IndexError, ValueError):
        pass
    lattice = []
    for _ in range(3):
        toks = lines[i].split()
        lattice.append([float(x) for x in toks[:3]])
        i += 1
    lattice = np.array(lattice, dtype=float) * scale
    # 元素行：可能是符号(VASP5) 或 数量(VASP4)。启发式判断。
    toks = lines[i].split()
    i += 1
    try:
        counts = [int(x) for x in toks]
        symbols = lines[i].split()
        i += 1
    except ValueError:
        symbols = toks
        counts = [int(x) for x in lines[i].split()]
        i += 1
    species = []
    for s, c in zip(symbols, counts):
        species.extend([s] * c)
    return dict(comment=comment, lattice=lattice, species=species, natoms=len(species))


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def magpie_stats(species, props):
    """对'每原子'元素列表(含重复)做 8 属性 unweighted 统计，与 atlas 脚本 37 一致。"""
    out = {}
    for p in PROP_NAMES:
        vals = []
        for sp in species:
            v = _num(props.get(sp, {}).get(p))
            if v is not None:
                vals.append(v)
        if not vals:
            out[f"{p}_mean"] = np.nan
            out[f"{p}_std"] = np.nan
            out[f"{p}_min"] = np.nan
            out[f"{p}_max"] = np.nan
            out[f"{p}_range"] = np.nan
            continue
        a = np.array(vals, dtype=float)
        out[f"{p}_mean"] = float(a.mean())
        out[f"{p}_std"] = float(a.std())
        out[f"{p}_min"] = float(a.min())
        out[f"{p}_max"] = float(a.max())
        out[f"{p}_range"] = float(a.max() - a.min())
    return out


def structural_stats(lattice, species, props):
    a1, a2, a3 = lattice
    n_z = a3 / (np.linalg.norm(a3) + 1e-12)
    c = float(abs(np.dot(a3, n_z)))
    area = float(abs(np.linalg.norm(np.cross(a1, a2))))
    aspect = c / np.sqrt(area) if area > 1e-12 else np.nan
    masses = []
    for sp in species:
        m = _num(props.get(sp, {}).get("atomic_mass"))
        if m is not None:
            masses.append(m)
    total_mass = float(sum(masses)) if masses else np.nan
    # density g/cm^3 = amu*1.66054e-24 g / (Å^3 * 1e-24 cm^3)
    vol = area * c
    density = (total_mass * 1.66054 / vol) if vol > 1e-12 else np.nan
    return dict(density=float(density), nsites=len(species),
                inplane_area=area, aspect_c_over_sqrtA=aspect,
                vacuum_c=c, inplane_area_raw=area)


def compute_features(poscar_text, elem_props):
    """POSCAR 文本 -> 14 维特征 dict(与 FEATURE_ORDER 同序)。"""
    p = parse_poscar(poscar_text)
    m = magpie_stats(p["species"], elem_props)
    s = structural_stats(p["lattice"], p["species"], elem_props)
    feat = {}
    feat["electronegativity_mean"] = m["electronegativity_mean"]
    feat["electronegativity_range"] = m["electronegativity_range"]
    feat["atomic_mass_mean"] = m["atomic_mass_mean"]
    feat["atomic_mass_max"] = m["atomic_mass_max"]
    feat["atomic_radius_mean"] = m["atomic_radius_mean"]
    feat["Z_mean"] = m["Z_mean"]
    feat["ionization_energy_mean"] = m["ionization_energy_mean"]
    feat["electron_affinity_mean"] = m["electron_affinity_mean"]
    feat["row_mean"] = m["row_mean"]
    feat["group_range"] = m["group_range"]
    feat["density"] = s["density"]
    feat["nsites"] = s["nsites"]
    feat["inplane_area"] = s["inplane_area"]
    feat["aspect_c_over_sqrtA"] = s["aspect_c_over_sqrtA"]
    return feat, dict(species=p["species"], lattice=p["lattice"].tolist())


def feature_vector(feat_dict):
    """按 FEATURE_ORDER 取 14 维向量(缺失置 nan)。"""
    return np.array([feat_dict.get(k, np.nan) for k in FEATURE_ORDER], dtype=float)
