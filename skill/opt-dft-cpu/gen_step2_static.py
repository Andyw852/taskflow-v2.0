#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step2_static.py — 静态自洽：从 step1_opt 的 CONTCAR 接力，输出体系总能。

继承 step1 的泛函（workflow_method.txt 的 FUNC/GGA/IVDW）与磁性（ISPIN/MAGMOM），
把 INCAR 里的弛豫键（EDIFFG/POTIM/ISIF/IOPTCELL）换成静态自洽键（IBRION=-1/NSW=0）。
KPOINTS 默认按优化后的结构用 VASPKIT 重新生成（2D 压真空方向为 1，0D 只用 Γ）。

Slurm 参数（partition / nodes / qos / VASP 路径）在 submit_std_{2d,3d}.tpl 里写死，
本脚本只填充 {{JOBNAME}}。

用法（在材料目录下）：
    python gen_step2_static.py
    python gen_step2_static.py --no-vaspkit    # 复用 step1 的 KPOINTS
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (force_kz1, require_dim, resolve_dim,  # noqa: E402
                        resolve_tpl, validate_poscar)

STEP1_DIR = "step1_opt"
STEP2_DIR = "step2_static"
INCAR_FILE = "INCAR"
POTCAR_FILE = "POTCAR"
METHOD_FILE = "workflow_method.txt"
KSCHEME = "2"
KSPACING = "0.03"
SUPPORTED_FUNCS = ("pbe-d3", "pbesol", "pbe")

# ---- 静态自洽的关键参数 ----
# 粗筛档：与 S1 弛豫同一标准（0.05 eV/Å + EDIFF 1e-4）。对相对稳定性排名足够，
# 关心的能量差（异构体差、形成能）远大于 1e-4 eV 的 SCF 噪声。
# ★ 若要某几个候选的“绝对”形成能达发表级，单独把静态收紧到 1E-5~1E-6（静态只是单点，很便宜）。
STEP2_EDIFF = "1E-4"
STEP2_ISYM = "2"         # 自洽用对称约化后的不可约 k 点集，开对称化

INCAR_REMOVE = {"EDIFFG", "POTIM", "ISIF", "IOPTCELL"}
INCAR_SET_BASE = {
    "EDIFF": STEP2_EDIFF,
    "IBRION": "-1",
    "NSW": "0",
    "ISYM": STEP2_ISYM,
    "LREAL": ".FALSE.",      # 静态出精确可比总能，用倒空间投影（不管 S1 弛豫用的 Auto）
    "LORBIT": "11",
    "LWAVE": ".FALSE.",
    "LCHARG": ".TRUE.",
}

MAG_ZERO_TOL = 0.1        # |磁矩| 低于此视作已塌缩到非磁


def parse_incar(path):
    """读 INCAR -> [(KEY, value), ...]，保留顺序。"""
    items = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            s = line.strip()
            if not s or s[0] in "#!":
                continue
            for marker in ("#", "!"):
                if marker in s:
                    s = s.split(marker, 1)[0].strip()
            if "=" not in s:
                continue
            for part in s.split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    items.append((key.strip().upper(), value.strip()))
    return items


def detect_method(items, method_file: Path = None):
    """优先读 workflow_method.txt 的 FUNC=；缺失时回退为嗅探 INCAR。"""
    if method_file is not None and method_file.exists():
        for line in method_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("FUNC="):
                func = line.split("=", 1)[1].strip()
                if func in SUPPORTED_FUNCS:
                    return func
                break
    data = {key: value for key, value in items}
    gga = data.get("GGA", "").upper()
    ivdw = data.get("IVDW", "").split()[0] if data.get("IVDW") else None
    if gga == "PS" and ivdw is None:
        return "pbesol"
    if gga == "PE" and ivdw == "12":
        return "pbe-d3"
    if gga == "PE" and ivdw is None:
        return "pbe"
    raise SystemExit(
        "[ERROR] step1 泛函不在支持列表内。检测到 GGA=%s、IVDW=%s。"
        "支持：%s。" % (gga or "(缺失)", ivdw or "(无)", ", ".join(SUPPORTED_FUNCS)))


def read_species_and_counts(path: Path):
    """从 POSCAR 读 (元素符号列表, 各元素原子数)。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    line6 = lines[5].split()
    if line6 and line6[0].lstrip("-").isdigit():
        return None, [int(x) for x in line6]
    return line6, [int(x) for x in lines[6].split()]


def read_magnetization(outcar: Path, nions: int):
    """读 OUTCAR 最后一个 'magnetization (x)' 块里每离子 tot 磁矩；无则 None。"""
    if not outcar.exists():
        return None
    lines = outcar.read_text(errors="ignore").splitlines()
    last = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("magnetization (x)"):
            last = i
    if last is None:
        return None
    moms, seen = [], False
    for ln in lines[last + 1:]:
        t = ln.split()
        if t and t[0].isdigit():
            moms.append(float(t[-1]))
            seen = True
        elif seen:
            break
    return moms[:nions] if len(moms) >= nions else None


def decide_magnetism(items, step1_moms):
    """静态步的磁性策略：继承 step1 的 ISPIN；有收敛磁矩则刷新 MAGMOM，
    磁矩塌缩到 ≈0 则降级为非磁。返回 (magnetic, magmom_str_or_None, note)。"""
    ispin = 1
    for key, value in items:
        if key == "ISPIN":
            try:
                ispin = int(value.split()[0])
            except ValueError:
                pass
    if ispin != 2:
        return False, None, "非磁（继承 step1 ISPIN=1）"
    if step1_moms is not None:
        mmax = max((abs(m) for m in step1_moms), default=0.0)
        if mmax <= MAG_ZERO_TOL:
            return False, None, "step1 磁矩已塌缩(max|m|=%.3f)，降级非磁 ISPIN=1" % mmax
        magmom = " ".join("%g" % round(m, 3) for m in step1_moms)
        return True, magmom, "继承 step1 收敛磁矩(max|m|=%.2f)" % mmax
    return True, None, "磁性（继承 step1 MAGMOM）"


def build_incar(src_items, remove, set_values):
    remove = {key.upper() for key in remove}
    set_values = {key.upper(): value for key, value in set_values.items()}
    body, seen = [], set()
    for key, value in src_items:
        if key in remove or key == "SYSTEM":
            continue
        body.append((key, set_values.get(key, value)))
        seen.add(key)
    for key, value in set_values.items():
        if key != "SYSTEM" and key not in seen:
            body.append((key, value))
    lines = ["SYSTEM = %s" % set_values["SYSTEM"]]
    lines.extend("%-8s = %s" % (key, value) for key, value in body)
    return "\n".join(lines) + "\n"


def render_submit(tpl_path, out_path, params):
    if not os.path.exists(tpl_path):
        raise SystemExit("[ERROR] 缺少提交模板：%s" % tpl_path)
    text = Path(tpl_path).read_text(encoding="utf-8")
    for key, value in params.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftover = set(re.findall(r"\{\{(\w+)\}\}", text))
    if leftover:
        raise SystemExit("[ERROR] 提交模板 %s 有未填充占位符：%s"
                         % (tpl_path, leftover))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")


def run_vaspkit_kpoints(exe, outdir, kscheme, kspacing):
    print("[..] 重新生成 KPOINTS：1 -> 102 -> %s -> %s" % (kscheme, kspacing))
    subprocess.run([exe], input="1\n102\n%s\n%s\n" % (kscheme, kspacing),
                   text=True, cwd=outdir, check=True)


def sanitize_label(text: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return label.strip("_.-") or "material"


def read_structure_label(path: Path) -> str:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return "material"
    token = lines[0].split()[0] if lines[0].split() else "material"
    return sanitize_label(token)


def parse_args():
    p = argparse.ArgumentParser(description="生成 opt-dft-cpu step2 静态自洽输入")
    p.add_argument("--vaspkit", default="vaspkit")
    p.add_argument("--no-vaspkit", action="store_true",
                   help="复用 step1 的 KPOINTS（不重新生成）")
    p.add_argument("--kscheme", default=KSCHEME)
    p.add_argument("--kspacing", default=KSPACING)
    p.add_argument("--jobname", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    step1 = Path(STEP1_DIR)
    step2 = Path(STEP2_DIR)
    if not step1.is_dir():
        sys.exit("[ERROR] 缺少 %s；请在材料目录下运行" % STEP1_DIR)

    incar_path = step1 / INCAR_FILE
    if not incar_path.exists():
        sys.exit("[ERROR] 缺少 %s" % incar_path)

    struct = next((step1 / name for name in ("CONTCAR", "POSCAR")
                   if (step1 / name).exists()), None)
    if struct is None:
        sys.exit("[ERROR] %s 里没有 CONTCAR/POSCAR" % STEP1_DIR)
    bad = validate_poscar(struct)
    if bad:
        sys.exit("[ERROR] %s 不完整：%s。\n"
                 "        step1 弛豫可能还在跑（CONTCAR 写了一半）——\n"
                 "        等 tf 里 S1 变 done 再生成 S2。" % (struct, bad))

    items = parse_incar(incar_path)
    method = detect_method(items, step1 / METHOD_FILE)
    label = read_structure_label(struct)
    step2.mkdir(exist_ok=True)

    Path(step2 / "POSCAR").write_text(
        struct.read_text(encoding="utf-8-sig"), encoding="utf-8", newline="\n")
    if struct.name != "CONTCAR":
        print("[WARN] step1 没有 CONTCAR，回退用 POSCAR")

    # ---- 维度：继承 step1 workflow_method.txt 的 DIM=，缺失按结构判定 ----
    dim, dim_note = resolve_dim(step1 / METHOD_FILE, step2 / "POSCAR")
    require_dim(dim, ("0d", "2d", "3d"), "step2_static")
    submit_tpl = resolve_tpl(Path.cwd(), "submit_std", dim)
    print("[..] 维度：%s — %s" % (dim.upper(), dim_note))

    potcar_src = step1 / POTCAR_FILE
    if not potcar_src.exists():
        sys.exit("[ERROR] 缺少 %s" % potcar_src)
    shutil.copyfile(potcar_src, step2 / POTCAR_FILE)

    if dim == "0d":
        (step2 / "KPOINTS").write_text(
            "Gamma only (0D molecule)\n0\nGamma\n1 1 1\n0 0 0\n",
            encoding="utf-8", newline="\n")
        print("[OK] KPOINTS: Gamma only (0D)")
    elif args.no_vaspkit:
        old_kpoints = step1 / "KPOINTS"
        if not old_kpoints.exists():
            sys.exit("[ERROR] --no-vaspkit 但 step1 没有 KPOINTS")
        shutil.copyfile(old_kpoints, step2 / "KPOINTS")
        print("[WARN] 复用 step1 KPOINTS；ISIF=3 变胞后建议重新生成")
    else:
        try:
            run_vaspkit_kpoints(args.vaspkit, step2, args.kscheme, args.kspacing)
            if dim == "2d":
                changed, note = force_kz1(step2 / "KPOINTS")
                print("[%s] 2D KPOINTS 真空方向细分：%s" % ("OK" if changed else "..", note))
        except FileNotFoundError:
            sys.exit("[ERROR] 找不到 VASPKIT：%s" % args.vaspkit)
        except subprocess.CalledProcessError as exc:
            sys.exit("[ERROR] VASPKIT 失败，返回码=%s" % exc.returncode)

    jobname = args.jobname or sanitize_label("%s_s2static" % label)[:80]
    render_submit(str(submit_tpl), step2 / "submit.sh", {"JOBNAME": jobname})

    # ---- 磁性：继承 step1 状态，刷新收敛磁矩 ----
    symbols, counts = read_species_and_counts(step2 / "POSCAR")
    nions = sum(counts)
    step1_moms = read_magnetization(step1 / "OUTCAR", nions)
    magnetic, magmom, mag_note = decide_magnetism(items, step1_moms)

    incar_set = dict(INCAR_SET_BASE)
    incar_set["SYSTEM"] = "%s %s static" % (label, method)
    incar_remove = set(INCAR_REMOVE)
    if magnetic:
        incar_set["ISPIN"] = "2"
        if magmom is not None:
            incar_set["MAGMOM"] = magmom
        # magmom=None：原样继承 step1 INCAR 的 MAGMOM
    else:
        incar_set["ISPIN"] = "1"
        incar_remove.add("MAGMOM")
        incar_remove.add("NUPDOWN")
    (step2 / "INCAR").write_text(
        build_incar(items, incar_remove, incar_set), encoding="utf-8", newline="\n")

    method_src = step1 / METHOD_FILE
    if method_src.exists():
        shutil.copyfile(method_src, step2 / METHOD_FILE)
    else:
        (step2 / METHOD_FILE).write_text(
            "FUNC=%s\nLABEL=%s\nDIM=%s\n" % (method, label, dim.upper()),
            encoding="utf-8", newline="\n")

    final = dict(parse_incar(step2 / "INCAR"))
    print("结构     : %s -> %s" % (struct, step2 / "POSCAR"))
    print("方法     : %s (GGA=%s, IVDW=%s)" % (method, final.get("GGA"),
                                              final.get("IVDW", "off")))
    print("维度     : %s (%s)" % (dim.upper(), dim_note))
    print("磁性     : %s — %s" % ("ON" if magnetic else "OFF", mag_note))
    if magnetic and final.get("MAGMOM"):
        print("           MAGMOM = %s" % final["MAGMOM"])
    print("KPOINTS  : %s" % ("复用" if args.no_vaspkit else "按优化结构重新生成"))
    print("作业名   : %s" % jobname)
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "submit.sh", METHOD_FILE):
        print("[%s] %s" % ("OK" if (step2 / name).exists() else "MISSING", name))
    print("\n[DONE] step2_static 已生成，可提交（vasp_std）")


if __name__ == "__main__":
    main()