#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu 核心（纯标准库，超算登录节点可跑）。

职责：POSCAR 解析/写出、超胞、缺陷枚举（层状结构按 (物种, z层) 去重）、
INCAR/KPOINTS/POTCAR/submit.sh 渲染。不依赖 pymatgen/ase。
"""
import os
import re
from pathlib import Path

# ---------------------------------------------------------------- POSCAR IO
def parse_poscar(path):
    L = Path(path).read_text(encoding="utf-8-sig").splitlines()
    comment = L[0].strip()
    scale = float(L[1].split()[0])
    lat = []
    for i in range(2, 5):
        lat.append([float(x) * scale for x in L[i].split()])
    syms = L[5].split()
    cnts = [int(x) for x in L[6].split()]
    mode = L[7].strip().lower()
    start = 8
    # skip selective dynamics line if present
    if len(L) > start and L[start].strip().lower().startswith(("s", "selective")):
        start += 1
    coords = []
    for line in L[start:start + sum(cnts)]:
        if not line.strip():
            continue
        coords.append([float(x) for x in line.split()[:3]])
    atoms = []
    for s, n in zip(syms, cnts):
        atoms += [s] * n
    # if cartesian, convert to fractional (direct)
    if mode.startswith("c") or mode.startswith("k"):
        # scale already applied to lattice; convert cartesian -> frac
        a = lat
        det = (a[0][0]*(a[1][1]*a[2][2]-a[1][2]*a[2][1])
               - a[0][1]*(a[1][0]*a[2][2]-a[1][2]*a[2][0])
               + a[0][2]*(a[1][0]*a[2][1]-a[1][1]*a[2][0]))
        inv = [[0.0]*3 for _ in range(3)]
        inv[0][0] = (a[1][1]*a[2][2]-a[1][2]*a[2][1])/det
        inv[0][1] = (a[0][2]*a[2][1]-a[0][1]*a[2][2])/det
        inv[0][2] = (a[0][1]*a[1][2]-a[0][2]*a[1][1])/det
        inv[1][0] = (a[1][2]*a[2][0]-a[1][0]*a[2][2])/det
        inv[1][1] = (a[0][0]*a[2][2]-a[0][2]*a[2][0])/det
        inv[1][2] = (a[0][2]*a[1][0]-a[0][0]*a[1][2])/det
        inv[2][0] = (a[1][0]*a[2][1]-a[1][1]*a[2][0])/det
        inv[2][1] = (a[0][1]*a[2][0]-a[0][0]*a[2][1])/det
        inv[2][2] = (a[0][0]*a[1][1]-a[0][1]*a[1][0])/det
        coords = [[inv[i][0]*c[0]+inv[i][1]*c[1]+inv[i][2]*c[2] for i in range(3)]
                  for c in coords]
    return {"comment": comment, "lat": lat, "atoms": atoms,
            "coords": [[c[0] % 1, c[1] % 1, c[2] % 1] for c in coords]}

def write_poscar(path, struct, comment=None):
    lat = struct["lat"]
    atoms = struct["atoms"]
    order = []
    for a in atoms:
        if a not in order:
            order.append(a)
    counts = [atoms.count(a) for a in order]
    idx = {a: i for i, a in enumerate(order)}
    lines = [comment or struct.get("comment", "defect"),
             "1.0",
             "  %16.10f  %16.10f  %16.10f" % tuple(lat[0]),
             "  %16.10f  %16.10f  %16.10f" % tuple(lat[1]),
             "  %16.10f  %16.10f  %16.10f" % tuple(lat[2]),
             "  " + "  ".join(order),
             "  " + "  ".join(str(counts[i]) for i in range(len(order))),
             "Direct"]
    grouped = [[] for _ in order]
    for a, c in zip(atoms, struct["coords"]):
        grouped[idx[a]].append(c)
    for g in grouped:
        for c in g:
            lines.append("  %16.10f  %16.10f  %16.10f" % tuple(c))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------------------------------------------------------------- 超胞 / 缺陷
def supercell(struct, n=(3, 3, 1)):
    lat = struct["lat"]
    new_lat = [[lat[i][j] * n[i] for j in range(3)] for i in range(3)]
    atoms, coords = [], []
    for a, c in zip(struct["atoms"], struct["coords"]):
        for i in range(n[0]):
            for j in range(n[1]):
                for k in range(n[2]):
                    atoms.append(a)
                    coords.append([(c[0] + i) / n[0],
                                   (c[1] + j) / n[1],
                                   (c[2] + k) / n[2]])
    return {"lat": new_lat, "atoms": atoms, "coords": coords}

def _zkey(c, tol=1e-3):
    z = c[2] % 1.0
    z = min(z, 1.0 - z)          # 折叠 z<->1-z（该结构有反演/镜面对称）
    return round(z / tol) * tol

def inequivalent_sites(struct):
    """按 (物种, z层) 去重，返回唯一站点的原子下标。

    仅对 P-3m1 层状 A2B2Te5 有效：同一 (物种, z层) 的多个原子属于同一 Wyckoff
    位点（2c/2d 的 2 个原子由 C3 旋转联系，对称等价），故按 z 去重即可得到 5 个
    不等价位。⚠️ 不通用：若换更低对称的结构（同 z 层存在不等价的 x/y 位），这里会
    错误合并——那种体系请改用含对称分析的库（pymatgen/ase）。"""
    seen, reps = {}, []
    for i, (a, c) in enumerate(zip(struct["atoms"], struct["coords"])):
        key = (a, _zkey(c))
        if key not in seen:
            seen[key] = i
            reps.append((i, key))
    return reps

def vdw_gap_z(struct):
    """旧实现（已废弃）：找最大分数 z 间隙中点，会把间隙原子压到离宿主 ~1 Å。
    保留仅为兼容；间隙缺陷请用 find_voids。"""
    zs = sorted(_zkey(c) for c in struct["coords"])
    best, best_w = 0.5, 0.0
    for z1, z2 in zip(zs, zs[1:]):
        if z2 - z1 > best_w:
            best, best_w = 0.5 * (z1 + z2), z2 - z1
    return best


def _frac_to_cart(frac, lat):
    return [sum(frac[k] * lat[k][j] for k in range(3)) for j in range(3)]


def _metric(lat):
    """metric 张量 G = lat·lat^T，用于由分数坐标差直接求笛卡尔距离平方。"""
    return [[sum(lat[i][k] * lat[j][k] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _min_dist_to_atoms(frac_pos, host_frac, G):
    """frac_pos(分数) 到所有宿主原子的最小镜像距离(Å)。分数差折叠到 [-0.5,0.5] + metric。"""
    best = 1e9
    for h in host_frac:
        df = [frac_pos[k] - h[k] for k in range(3)]
        df = [x - round(x) for x in df]
        d2 = sum(df[i] * G[i][j] * df[j] for i in range(3) for j in range(3))
        if d2 < best:
            best = d2
    return best ** 0.5


def _frac_sep(f1, f2, G):
    """两个分数坐标之间的最小周期距离(Å)。"""
    df = [f1[k] - f2[k] for k in range(3)]
    df = [x - round(x) for x in df]
    d2 = sum(df[i] * G[i][j] * df[j] for i in range(3) for j in range(3))
    return d2 ** 0.5


def find_voids(struct, n=2, grid=20, min_sep=2.0):
    """网格搜索 n 个空隙位置：离所有宿主原子的最小距离降序，且两两间距 > min_sep(Å)。
    返回 [(fx, fy, fz), ...]，每个都尽量远离所有宿主原子（用于间隙缺陷初始位置）。"""
    lat = struct["lat"]
    G = _metric(lat)
    host = struct["coords"]  # 分数坐标
    cands = []
    for i in range(grid):
        for j in range(grid):
            for k in range(grid):
                f = ((i + 0.5) / grid, (j + 0.5) / grid, (k + 0.5) / grid)
                cands.append((_min_dist_to_atoms(f, host, G), f))
    cands.sort(key=lambda x: -x[0])
    chosen = []
    for d, f in cands:
        if all(_frac_sep(f, cf, G) > min_sep for _, cf in chosen):
            chosen.append((d, f))
            if len(chosen) >= n:
                break
    return [f for _, f in chosen]

def remove_atom(struct, idx):
    st = {"lat": [r[:] for r in struct["lat"]],
          "atoms": struct["atoms"][:], "coords": [c[:] for c in struct["coords"]]}
    st["atoms"].pop(idx); st["coords"].pop(idx)
    return st

def substitute_atom(struct, idx, el):
    st = {"lat": [r[:] for r in struct["lat"]],
          "atoms": struct["atoms"][:], "coords": [c[:] for c in struct["coords"]]}
    st["atoms"][idx] = el
    return st

def insert_atom(struct, el, frac):
    st = {"lat": [r[:] for r in struct["lat"]],
          "atoms": struct["atoms"][:] + [el],
          "coords": [c[:] for c in struct["coords"]] + [list(frac)]}
    return st

def enumerate_defects(sc):
    """返回 [(dir_suffix, 显示名, 结构)]：空位 + 反位 + 阳离子互占位对 + 间隙。"""
    uniq = inequivalent_sites(sc)
    species = sorted({a for a in sc["atoms"]})
    out = []
    for idx, (a, z) in uniq:
        out.append(("v_%s" % a, "v_%s(z=%.3f)" % (a, z), remove_atom(sc, idx)))
    for idx, (a, z) in uniq:
        for b in species:
            if b != a:
                out.append(("%s_%s" % (b, a), "%s_%s(z=%.3f)" % (b, a, z),
                            substitute_atom(sc, idx, b)))
    # 阳离子互占位对：交换一个 A 位与一个 B 位（A/B 为两个阳离子物种）
    cats = [a for a in species if a != "Te"]
    if len(cats) >= 2:
        A, B = cats[0], cats[1]
        ia = next(i for i, a in enumerate(sc["atoms"]) if a == A)
        ib = next(i for i, a in enumerate(sc["atoms"]) if a == B)
        pair = substitute_atom(substitute_atom(sc, ia, B), ib, A)
        out.append(("%s_%s__%s_%s_pair" % (A, B, B, A), "%s_%s+%s_%s pair" % (A, B, B, A), pair))
    # 间隙：网格搜索找真正空隙（离宿主原子最远），各元素各放一个
    voids = find_voids(sc, n=2)
    for el in species:
        for tag, frac in zip(("a", "b"), voids):
            out.append(("%s_i_%s" % (el, tag), "%s_i@void%s" % (el, tag),
                        insert_atom(sc, el, frac)))
    return out

# ---------------------------------------------------------------- 输入渲染
def render_incar(tpl, mapping):
    text = Path(tpl).read_text(encoding="utf-8")
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text

def write_kpoints(path, mesh=(2, 2, 2)):
    text = ("Auto mesh (supercell)\n0\nGamma\n%d %d %d\n0 0 0\n" % mesh)
    Path(path).write_text(text, encoding="utf-8")

def potcar_variant(el):
    # 本体系推荐的 PAW：主族重元素用 _d（含 d 半芯态），Te/Sb/Bi 默认
    return {"Pb": "Pb_d", "Sn": "Sn_d", "Sb": "Sb", "Bi": "Bi_d", "Te": "Te"}.get(el, el)

def assemble_potcar(symbols, potcar_dir, variants=None, out_path="POTCAR"):
    """symbols 按 POSCAR 元素顺序；potcar_dir 下含 potpaw_PBE/POTCAR_<variant>。
    直接写到 out_path（避免先写 cwd 再移动导致的竞态）。"""
    potcar_dir = os.path.expanduser(str(potcar_dir))
    variants = variants or {}
    blocks = []
    for el in symbols:
        v = variants.get(el, potcar_variant(el))
        cands = [
            os.path.join(potcar_dir, "potpaw_PBE_64", v, "POTCAR"),   # jzzn 子目录布局
            os.path.join(potcar_dir, "potpaw_PBE_54", v, "POTCAR"),
            os.path.join(potcar_dir, "potpaw_PBE", v, "POTCAR"),      # 通用子目录
            os.path.join(potcar_dir, "potpaw_PBE", "POTCAR_" + v),    # 平铺文件
            os.path.join(potcar_dir, "potpaw_PBE.54", "POTCAR_" + v),
        ]
        p = next((c for c in cands if os.path.exists(c)), None)
        if p is None:
            raise SystemExit("[错误] 找不到 POTCAR(%s)：试过 %s（请核对 step.conf 的 POTCAR_DIR / variant）" % (v, potcar_dir))
        blocks.append(Path(p).read_text(encoding="utf-8"))
    # 完整拼接：每个 POTCAR 都以 TITEL 行开头，VASP 按块顺序读取，无需删行
    content = "".join(b if b.endswith("\n") else b + "\n" for b in blocks)
    Path(out_path).write_text(content, encoding="utf-8")

def render_submit(tpl_path, out_path, jobname):
    text = Path(tpl_path).read_text(encoding="utf-8")
    text = text.replace("{{JOBNAME}}", jobname)
    left = set(re.findall(r"\{\{(\w+)\}\}", text))
    if left:
        raise SystemExit("[错误] submit 模板仍有未填充占位符：%s" % left)
    Path(out_path).write_text(text, encoding="utf-8")

# ---------------------------------------------------------------- step.conf + 作业组装
def load_stepconf(path="step.conf"):
    import configparser
    c = configparser.ConfigParser(inline_comment_prefixes=("#",))
    c.optionxform = str          # 保留键名大小写（SUPERCELL 等）
    c.read(path)
    return {k: v.strip() for k, v in c.items("params")}

def parse_variant(conf):
    out = {}
    for item in conf.get("POTCAR_VARIANT", "").split(","):
        item = item.strip()
        if ":" in item:
            k, v = item.split(":", 1)
            out[k.strip()] = v.strip()
    return out

def find_incar_tpl():
    """优先 cwd（tf 从技能 templates/ 推到材料目录），其次技能目录 templates/。"""
    for d in (Path.cwd(), Path(__file__).resolve().parent / "templates"):
        p = d / "incar_defect.tpl"
        if p.exists():
            return str(p)
    raise SystemExit("[错误] 找不到 incar_defect.tpl")


def find_submit_tpl(use_ncl=True, dim="3d"):
    """优先 cwd（tf 从 setting/<hpc>/templates 推来），其次技能目录 templates/。"""
    base = "submit_ncl" if use_ncl else "submit_std"
    names = ["%s_%s.tpl" % (base, dim), "%s.tpl" % base]
    cwd = Path.cwd()
    skill = Path(__file__).resolve().parent / "templates"
    for d in (cwd, skill):
        for n in names:
            p = d / n
            if p.exists():
                return str(p)
    raise SystemExit("[错误] 找不到提交模板 %s（请在 setting/<hpc>/templates 或技能 templates/ 放置）" % names[0])

def sanitize_label(text):
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip()).strip("_.-") or "defect"

def build_job(outdir, struct, conf, incar_map, jobname, use_ncl=True):
    """在 outdir 下写 POSCAR/INCAR/KPOINTS/POTCAR/submit.sh。struct 需已排好元素序。"""
    os.makedirs(outdir, exist_ok=True)
    # 元素顺序（保持 POSCAR 一致）
    order = []
    for a in struct["atoms"]:
        if a not in order:
            order.append(a)
    write_poscar(os.path.join(outdir, "POSCAR"), struct, comment=incar_map.get("SYSTEM", "defect"))
    natoms = len(struct["atoms"])
    gga = "PE"
    vdw = "IVDW = 12" if conf.get("FUNC", "") == "pbe-d3" else ""
    m = dict(incar_map)
    m.setdefault("ENCUT", conf.get("ENCUT", "370"))
    m.setdefault("GGA", gga)
    m.setdefault("VDW_LINE", vdw)
    m.setdefault("IBRION", "-1"); m.setdefault("ISIF", "0"); m.setdefault("NSW", "0")
    m.setdefault("EDIFFG_LINE", "")
    m.setdefault("LVHAR_LINE", "")
    m.setdefault("ICHARG_LINE", "ICHARG = 2")
    m.setdefault("SIGMA_LINE", "SIGMA = 0.05")
    m.setdefault("MAGMOM", "%d*0" % (3 * natoms))  # SOC(LSORBIT)下每原子3分量(非共线)
    m.setdefault("NELECT_LINE", "")
    incar = render_incar(find_incar_tpl(), m)
    Path(os.path.join(outdir, "INCAR")).write_text(incar, encoding="utf-8")
    write_kpoints(os.path.join(outdir, "KPOINTS"), tuple(int(x) for x in conf.get("KMESH", "2 2 1").split()))
    assemble_potcar(order, conf["POTCAR_DIR"], parse_variant(conf),
                    os.path.join(outdir, "POTCAR"))
    tpl = find_submit_tpl(use_ncl)
    render_submit(tpl, os.path.join(outdir, "submit.sh"), sanitize_label(jobname))
    return order

def spin_seed_magmom(natoms, seed_atom=0, moment=1.0):
    """SOC(非共线)下给 seed_atom 一个初始磁矩，打破自旋对称。

    奇数电子数的带电态（q=±1,±3…）有未配对自旋（doublet），MAGMOM=0 会把
    它强行算成非磁（闭壳配对）→ 能量偏高。这里 seed 一个 1 μB 的初始矩让 SCF
    探索自旋极化分支（矩会弛豫到缺陷局域）。偶数电子态不受影响（矩自动归零）。
    返回 MAGMOM 字符串（每原子 mx my mz）。"""
    parts = []
    for i in range(natoms):
        parts.append("%.1f 0 0" % moment if i == seed_atom else "0 0 0")
    return " ".join(parts)

def read_nelect_from_potcar(path):
    """从 POTCAR 汇总价电子数（每个 POTCAR 块的 ZVAL 之和）。"""
    zvals = [float(x) for x in re.findall(r"ZVAL\s*=\s*([\d.]+)", Path(path).read_text(encoding="utf-8"))]
    # ZVAL 每个元素一个；POTCAR 串联后每个块一个 ZVAL
    # 直接读 'number of ions per type' 不可靠，这里返回每个 ZVAL 列表供调用方按 counts 加权
    return zvals