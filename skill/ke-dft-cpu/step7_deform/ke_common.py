# -*- coding: utf-8 -*-
"""ke_common.py —— ke-dft-cpu 技能新步骤（uniform / dfpt / deform / amset）的公共工具。

只依赖标准库 + dim_common（同目录，setup 已放好）。故意不碰 pymatgen，
让 gen 脚本在登录节点用系统 python 就能跑。VASPKIT 负责 KPOINTS/POTCAR。

放置：由 skill.yaml 的 gen_need 列出，随每个用它的步骤推到材料目录。
      ——因此本文件要复制进每个用到它的步骤源目录（step3_uniform、
        step5_dielect、step7_deform）。见 setup_ke.sh。
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (adaptive_parallel_tags, detect_dimension,  # noqa: E402
                        force_kz1, validate_poscar, VACUUM_MIN)

METHOD_FILE = "workflow_method.txt"


# --------------------------------------------------------------------------
# 维度
# --------------------------------------------------------------------------
def resolve_dim_for(poscar: Path, dimension="auto", vacuum_min=VACUUM_MIN):
    """返回 (dim, vac_axis)。dim ∈ {'2d','3d'}；vac_axis 仅 2D 有意义。"""
    mode = str(dimension).lower()
    if mode in ("2d", "3d"):
        return mode, (2 if mode == "2d" else None)
    dim, axis, vacs = detect_dimension(poscar, vacuum_min)
    if dim == "2d" and axis != 2:
        sys.exit("[ERROR] 检测到 2D 但真空不在 c 轴（在 %d 轴）。请把结构旋转成"
                 "真空沿第 3 个晶格矢量再重跑。" % axis)
    return dim, (axis if dim == "2d" else None)


def read_method_dim(method_file: Path):
    """从上一步的 workflow_method.txt 读 DIM=2D/3D（有就返回 '2d'/'3d'，无返回 None）。"""
    if not method_file.is_file():
        return None
    for ln in method_file.read_text(errors="ignore").splitlines():
        if ln.strip().upper().startswith("DIM="):
            v = ln.split("=", 1)[1].strip().lower()
            if v in ("2d", "3d"):
                return v
    return None


def write_method(path: Path, dim: str, note: str, func: str = None):
    # patch_ke_dag：多记一行 FUNC=，让本步的泛函能被更下游的步骤继续继承
    lines = ["DIM=%s" % dim.upper()]
    if func:
        lines.append("FUNC=%s" % func)
    lines.append("# %s" % note)
    path.write_text("\n".join(lines) + "\n",
                    encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# VASPKIT：KPOINTS / POTCAR / ENCUT
# --------------------------------------------------------------------------
def vaspkit_kpoints(outdir: Path, kscheme="2", kspacing="0.03",
                    exe="vaspkit", dim="3d", vac_axis=2, force_kz1_2d=True):
    """VASPKIT 1→102→scheme→kspacing 生成 KPOINTS；2D 把真空方向细分压回 1。"""
    print("[..] VASPKIT KPOINTS：1 -> 102 -> %s -> %s" % (kscheme, kspacing))
    subprocess.run([exe], input="1\n102\n%s\n%s\n" % (kscheme, kspacing),
                   text=True, cwd=outdir, check=True)
    if dim == "2d" and force_kz1_2d:
        changed, note = force_kz1(outdir / "KPOINTS", axis=vac_axis if vac_axis is not None else 2)
        print("[%s] 2D KPOINTS 真空方向细分：%s" % ("OK" if changed else "..", note))


def vaspkit_potcar(outdir: Path, exe="vaspkit"):
    if (outdir / "POTCAR").exists():
        print("[OK] POTCAR 已存在，跳过")
        return
    print("[..] VASPKIT POTCAR：1 -> 103")
    subprocess.run([exe], input="1\n103\n", text=True, cwd=outdir, check=True)


def encut_from_potcar(potcar: Path, factor=1.5, fallback=300):
    vals = []
    for ln in potcar.read_text(errors="ignore").splitlines():
        m = re.search(r"ENMAX\s*=\s*([\d.]+)", ln)
        if m:
            vals.append(float(m.group(1)))
    if not vals:
        return int(fallback)
    import math
    return int(math.ceil(max(vals) * factor / 10.0) * 10)


# --------------------------------------------------------------------------
# INCAR 模板渲染 + 键改写
# --------------------------------------------------------------------------
def render_tpl(tpl_path: Path, subs: dict, out_path: Path):
    """把模板里的 {{KEY}} 占位符替换成 subs[KEY]，写出。"""
    text = tpl_path.read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = re.findall(r"\{\{([A-Z_]+)\}\}", text)
    if left:
        sys.exit("[ERROR] 模板 %s 还有未填占位符：%s" % (tpl_path.name, ", ".join(set(left))))
    out_path.write_text(text, encoding="utf-8", newline="\n")
    print("[OK] %s" % out_path.name)


# ---- [PATCH-IONRELAX] 应变配对反解（gen 与 step7b 共用，单一真源）----
def read_lattice_matrix(poscar):
    """读 POSCAR 晶格矩阵（3x3，含 scale）。纯 numpy，不碰 pymatgen。"""
    import numpy as np
    ln = Path(poscar).read_text().splitlines()
    scale = float(ln[1].split()[0])
    rows = [[float(x) for x in ln[i].split()[:3]] for i in (2, 3, 4)]
    return np.array(rows) * scale


def resolve_strain_pairs(out):
    """反解 xx±/yy± 形变配对（纯 numpy，与 step7b 的 _be_code 同源同公式）。

    形变梯度 F = (und_lat⁻¹ · def_lat)ᵀ（列矢量约定，与 amset calculate_deformation
    一致）；Green-Lagrange 应变 E = (FᵀF - I)/2；对角元最大者即主形变轴。
    返回 (pairs, strain_mag)：pairs={"xx":[plus,minus],"yy":[plus,minus]}（目录名），
    strain_mag={"xx":γxx,"yy":γyy}（工程应变）。反解不完整返回 (None, None)。
    gen_step9_deform.py 建 ionrelax/ 与 step7b 找 ionrelax/ 必须走这一个函数，
    否则两边各自反解会错位（gen 建 01/02、step7b 翻 03/04，静默降级）。"""
    import glob
    import numpy as np
    und_lat = read_lattice_matrix(Path(out) / "undeformed" / "POSCAR")
    pairs = {"xx": [None, None], "yy": [None, None]}
    strain_mag = {"xx": 0.0, "yy": 0.0}
    for d in sorted(glob.glob(os.path.join(str(out), "deform-*"))):
        p = os.path.join(d, "POSCAR")
        if not os.path.isfile(p):
            continue
        F = np.transpose(np.dot(np.linalg.inv(und_lat), read_lattice_matrix(p)))
        E = (np.dot(F.T, F) - np.eye(3)) / 2.0
        diag = [float(E[i, i]) for i in range(3)]
        idx = int(np.argmax(np.abs(diag)))
        val = diag[idx]
        if abs(val) < 1e-6:
            continue
        sign = 0 if val > 0 else 1
        if idx == 0:
            pairs["xx"][sign] = os.path.basename(d)
            strain_mag["xx"] = abs(F[idx, idx] - 1.0)
        elif idx == 1:
            pairs["yy"][sign] = os.path.basename(d)
            strain_mag["yy"] = abs(F[idx, idx] - 1.0)
    if None in pairs["xx"] or None in pairs["yy"]:
        return None, None
    return pairs, strain_mag


def apply_parallel_tags(incar_path):
    """按宿主机行级改写 INCAR 的 NCORE/KPAR（渲染后调用，模板里写死的值也能覆盖）。

    判定来自 dim_common.adaptive_parallel_tags()（单一真源）：
      GPU 版 → NCORE=1/KPAR=1；CPU → 不改（返回 False）。
    返回 True 表示已改写，False 表示未动（CPU 或文件缺失）。
    """
    tags = adaptive_parallel_tags()
    if not tags:
        return False
    p = Path(incar_path)
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8", errors="ignore")
    out, seen = [], set()
    for ln in text.splitlines():
        m = re.match(r"\s*([A-Za-z_]+)\s*=", ln)
        if m and m.group(1).upper() in tags:
            k = m.group(1).upper()
            out.append("%-8s = %s" % (k, tags[k]))
            seen.add(k)
            continue
        out.append(ln)
    for k, v in tags.items():
        if k not in seen:
            out.append("%-8s = %s" % (k, v))
    p.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print("[..] 并行参数已按宿主机覆盖：%s" % ", ".join("%s=%s" % kv for kv in tags.items()))
    return True


# ---- [PATCH-UCONS] 自旋 / U 标签继承 ----------------------------------
# step3_uniform / step5_dielect / step7_deform 的 INCAR 是纯模板渲染，模板里
# 没有 ISPIN/MAGMOM/LMAXMIX/LDAU* —— 结构按 +U + 自旋极化弛豫，输运/介电/形变势
# 却按裸 GGA 非自旋极化算，两者不是同一个哈密顿量。这里从上游步骤把这些标签接过来。
#
# ★ 上游 ISPIN!=2 且没有 LDAU 时【完全不动 INCAR】，非磁无 U 体系零改动。
# ★ 关掉：export TF_KE_NO_SCF_INHERIT=1
SCF_SPIN_TAGS = ("ISPIN", "MAGMOM", "NUPDOWN", "LMAXMIX")
SCF_U_TAGS = ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ", "LDAUPRINT")
SCF_SRC_DIRS = ("step2_bandgap/step2.1_static", "step1_opt",
                "step1_std_opt", "step1_PBE_opt")


def find_scf_source(cwd):
    """上游 SCF 的 INCAR：优先 step2.1_static（那里的 MAGMOM 是收敛值），
    回退 step1（初始高自旋猜测）。都没有返回 None。"""
    for rel in SCF_SRC_DIRS:
        p = Path(cwd) / rel / "INCAR"
        if p.is_file():
            return p
    return None


def inherit_scf_tags(incar_path, cwd, with_u=True, label=""):
    """把上游的自旋/U 标签注入已渲染好的 INCAR。返回注入的键列表（空 = 没动）。"""
    if os.environ.get("TF_KE_NO_SCF_INHERIT"):
        return []
    src = find_scf_source(cwd)
    if src is None:
        return []
    up = parse_incar(src.read_text(encoding="utf-8", errors="ignore"))
    ispin2 = str(up.get("ISPIN", "1")).split()[0] == "2"
    u_on = str(up.get("LDAU", "")).upper().lstrip(".").startswith("T")
    if not ispin2 and not u_on:
        return []                      # 非磁 + 无 U：什么都不用做
    want = list(SCF_SPIN_TAGS) + (list(SCF_U_TAGS) if with_u else [])
    take = {k: up[k] for k in want if k in up}
    if not ispin2:
        take.pop("MAGMOM", None)
        take.pop("NUPDOWN", None)
    if u_on and take.get("LMAXMIX") is None:
        take["LMAXMIX"] = "4"          # 加 U 的 d/f 混合需要 LMAXMIX>=4
    if not take:
        return []
    p = Path(incar_path)
    keep = [ln for ln in p.read_text(encoding="utf-8").splitlines()
            if not re.match(r"\s*(%s)\s*=" % "|".join(want), ln, re.IGNORECASE)]
    keep.append("")
    keep.append("# ---- 以下由 gen 脚本从 %s 继承（自旋/U 必须与弛豫时一致）----"
                % src.parent.as_posix())
    for k in want:
        if k in take:
            keep.append("%-10s = %s" % (k, take[k]))
    if u_on and not with_u:
        keep.append("# 注意：上游带 LDAU，但本步是 DFPT(IBRION=8)，多数 VASP 版本")
        keep.append("#       不支持 LDA+U 的 DFPT —— 这里【故意没有】继承 LDAU*。")
        keep.append("#       要带 U 的介电常数请改走 IBRION=6 有限差分。")
    p.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    tag = (" [%s]" % label) if label else ""
    print("[..] 继承上游自旋/U 标签%s：%s" % (tag, ", ".join(sorted(take))))
    if u_on and not with_u:
        print("     （本步是 DFPT，未继承 LDAU*；见 INCAR 末尾注释）")
    return sorted(take)


def parse_incar(text: str):
    d = {}
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].split("!", 1)[0].strip()
        if "=" in ln:
            k, v = ln.split("=", 1)
            d[k.strip().upper()] = v.strip()
    return d


def incar_text(d: dict, system="calc"):
    out = ["SYSTEM = %s" % system, ""]
    for k, v in d.items():
        if k == "SYSTEM":
            continue
        out.append("%-10s = %s" % (k, v))
    return "\n".join(out) + "\n"


def merge_incar(base: dict, overrides: dict):
    """overrides 里 value 为 None 表示删除该键。"""
    d = dict(base)
    for k, v in overrides.items():
        k = k.upper()
        if v is None:
            d.pop(k, None)
        else:
            d[k] = str(v)
    return d


# --------------------------------------------------------------------------
# 结构接力
# --------------------------------------------------------------------------
def relay_poscar(prev_contcar: Path, dst_poscar: Path, label="上一步"):
    """把上一步 CONTCAR 拷成本步 POSCAR；缺失就报错退出（绝不静默用旧结构）。"""
    if not prev_contcar.is_file():
        sys.exit("[ERROR] %s 的 CONTCAR 不存在：%s\n"
                 "        请确认上一步已完成再生成本步。" % (label, prev_contcar))
    validate_poscar(prev_contcar)
    shutil.copyfile(prev_contcar, dst_poscar)
    print("[OK] POSCAR ← %s" % prev_contcar)


def find_prev_dir(cwd: Path, candidates):
    """按顺序找第一个存在且有 CONTCAR 的目录名。"""
    for name in candidates:
        d = cwd / name
        if (d / "CONTCAR").is_file():
            return d
    return None


def new_jobname(cwd: Path, step_label: str):
    return "%s-ke-dft-cpu-%s" % (cwd.name, step_label)


def patch_submit_jobname(submit: Path, jobname: str):
    text = submit.read_text(encoding="utf-8")
    text = text.replace("{{JOBNAME}}", jobname)
    submit.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------
# patch_ke_dag：泛函继承（从 step1 的 workflow_method.txt 读 FUNC=）
# --------------------------------------------------------------------------
# 为什么要继承：几何是 step1 用某个泛函优化出来的，下游单点用同一泛函才自洽。
#   pbesol -> pbesol      同一套，论文里一句话说得清
#   pbe    -> pbe
#   pbe-d3 -> pbe-d3      D3 只加在总能/力/应力上，不进 KS 哈密顿量，
#                         所以对 uniform / deform 的本征值是恒等操作（保留只为
#                         INCAR 一致）；对 elastic-dft-cpu 是必须保留的（应力有贡献）。
# 例外：DFPT（IBRION=8）。VASP 手册明确写了 vdW 修正不进 DFPT 声子响应，
#   写上 IVDW 只会污染总能而不改 ε₀ 的离子部分，所以 step5 默认剥掉，
#   由 gen 脚本的 KEEP_D3_IN_DFPT 控制。
FUNC_MAP = {
    "pbe": {"GGA": "PE", "IVDW": None,
            "VDW_LINE": "# IVDW disabled: plain PBE"},
    "pbesol": {"GGA": "PS", "IVDW": None,
               "VDW_LINE": "# IVDW disabled: PBEsol"},
    "pbe-d3": {"GGA": "PE", "IVDW": "12",
               "VDW_LINE": "IVDW   = 12            # PBE + DFT-D3(BJ)"},
}
SUPPORTED_FUNCS = tuple(FUNC_MAP)


def read_method_func(method_file: Path):
    """从 workflow_method.txt 读 FUNC=；读不到或不认识返回 None。"""
    if not Path(method_file).is_file():
        return None
    for ln in Path(method_file).read_text(errors="ignore").splitlines():
        if ln.strip().upper().startswith("FUNC="):
            v = ln.split("=", 1)[1].strip().lower()
            return v if v in FUNC_MAP else None
    return None


def sniff_func_from_incar(incar: Path):
    """兜底：从 step1 的 INCAR 反推 GGA/IVDW。推不出返回 None。"""
    if not Path(incar).is_file():
        return None
    gga, ivdw = "", None
    for ln in Path(incar).read_text(errors="ignore").splitlines():
        ln = ln.split("#", 1)[0].split("!", 1)[0].strip()
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip().upper(), v.strip()
        if k == "GGA":
            gga = v.upper()
        elif k == "IVDW":
            ivdw = v.split()[0] if v.split() else None
    if gga == "PS" and ivdw is None:
        return "pbesol"
    if gga == "PE" and ivdw == "12":
        return "pbe-d3"
    if gga == "PE" and ivdw is None:
        return "pbe"
    return None


def resolve_func(prev_dir: Path, setting, step_name, drop_d3=False,
                 fallback="pbe"):
    """定出本步用的泛函，返回 (func, subs)。

    setting = "inherit" 时从 prev_dir/workflow_method.txt 读，读不到就嗅探
    prev_dir/INCAR，再读不到用 fallback 并告警。
    setting 直接写死泛函名时原样采用。
    drop_d3=True 会把 pbe-d3 降级成 pbe（DFPT 专用）。
    subs 是给 render_tpl 的占位符字典：{"GGA": ..., "VDW_LINE": ...}
    """
    s = str(setting).lower()
    if s == "inherit":
        func = read_method_func(Path(prev_dir) / METHOD_FILE)
        src = "workflow_method.txt"
        if func is None:
            func = sniff_func_from_incar(Path(prev_dir) / "INCAR")
            src = "嗅探 step1/INCAR"
        if func is None:
            func, src = fallback, "都读不到，回退默认值"
            print("[WARN] %s：无法从 %s 判定泛函，回退 FUNC=%s。"
                  "若 step1 是 pbesol/pbe-d3，结果会不自洽！"
                  % (step_name, prev_dir, func))
    elif s in FUNC_MAP:
        func, src = s, "脚本内写死"
    else:
        sys.exit("[ERROR] %s：FUNC=%r 无效，只允许 inherit / %s"
                 % (step_name, setting, " / ".join(FUNC_MAP)))

    eff = func
    if drop_d3 and func == "pbe-d3":
        eff = "pbe"
        print("[..] %s：DFPT 不支持 vdW 修正（VASP 手册），"
              "pbe-d3 -> pbe（几何仍是 D3 优化的）" % step_name)
    m = FUNC_MAP[eff]
    print("[..] %s：泛函 %s（来源：%s）-> GGA=%s IVDW=%s"
          % (step_name, eff, src, m["GGA"], m["IVDW"] or "off"))
    return eff, {"GGA": m["GGA"], "VDW_LINE": m["VDW_LINE"]}
