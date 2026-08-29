#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step2_static.py — build a consistent semilocal static calculation

This script inherits the geometry method selected in step1:
  pbe-d3 : GGA=PE and IVDW=12 are retained
  pbesol : GGA=PS and no IVDW is retained
  pbe    : GGA=PE and no IVDW is retained

Method inheritance now reads step1/workflow_method.txt first and only
falls back to sniffing step1/INCAR when the method file is missing.

Slurm parameters (partition / nodes / qos / VASP path) are hard-coded in
submit_std.tpl; this script only fills {{JOBNAME}}.

KPOINTS are regenerated from the optimized CONTCAR by default.

Usage:
    python gen_step2_static.py
    python gen_step2_static.py --no-vaspkit   # reuse step1 KPOINTS
    python gen_step2_static.py --jobname my_job
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import (require_dim, force_kz1, resolve_dim, resolve_tpl,  # noqa: E402
                        validate_poscar, adaptive_parallel_tags)
import stepconf  # noqa: E402

# step1 目录。"auto" = 自动找最后一个跑完的弛豫阶段：
#   step1c_PBE_opt -> step1b_PBE_opt -> step1a_PBE_opt -> step1_PBE_opt（旧的单目录）
# 也可以直接写死某个目录名。
STEP1_DIR = "auto"
STEP2_DIR = "step2_bandgap/step2.1_static"
INCAR_FILE = "INCAR"
POTCAR_FILE = "POTCAR"
METHOD_FILE = "workflow_method.txt"
KSCHEME = "2"
KSPACING = "0.03"

SUBMIT_TPL = "submit_std.tpl"

# ---- submit.sh Slurm 参数覆盖（渲染模板后再补丁；None=不改，保持模板原值）----
# submit.sh 来源不变（仍从 submit_std 模板渲染）；这里只在渲染后覆盖三行：
#   #SBATCH --nodes= / --ntasks-per-node= / --qos=
SUBMIT_OVERRIDE = {
    "nodes":           None,
    "ntasks_per_node": None,
    "qos":             None,
}
SUPPORTED_FUNCS = ("pbe-d3", "pbesol", "pbe")

# ---- 磁性自动处理 ----
# "auto": 依次判定——
#   1) MAGMOM_OVERRIDE 非空          -> 磁性，用手动值；
#   2) step1 是 ISPIN=2 且 OUTCAR 有收敛磁矩：
#        max|m| >  MAG_ZERO_TOL      -> 磁性，MAGMOM 刷新为 step1 收敛的逐离子磁矩
#                                        （保号、保 AFM，比高自旋起点收敛快得多）；
#        max|m| <= MAG_ZERO_TOL      -> 已塌缩到非磁解，自动降级 ISPIN=1 并剔除 MAGMOM；
#   3) step1 是 ISPIN=2 但读不到 OUTCAR 磁矩 -> 磁性，原样继承 step1 的 MAGMOM；
#   4) step1 非磁但 POSCAR 含磁性候选元素   -> 磁性，按元素表给高自旋起点并告警
#                                             （自愈：step1 大概是用旧脚本生成的）；
#   5) 其余                                 -> 非磁 ISPIN=1。
# True / False: 强制磁性 / 强制非磁。
AUTO_MAG = "auto"
MAGMOM_OVERRIDE = {}          # 例: {"Mn": 5.0, "In": 0.0, "Se": 0.0}
MAG_ZERO_TOL = 0.1            # |磁矩| 低于此视作 0
MAG_ELEM_MOMENTS = {
    "Sc": 1.0, "Ti": 1.0, "V": 3.0, "Cr": 4.0, "Mn": 5.0,
    "Fe": 4.0, "Co": 3.0, "Ni": 2.0, "Cu": 1.0,
    "Ce": 1.0, "Pr": 2.0, "Nd": 3.0, "Pm": 4.0, "Sm": 5.0, "Eu": 7.0,
    "Gd": 7.0, "Tb": 6.0, "Dy": 5.0, "Ho": 4.0, "Er": 3.0, "Tm": 2.0, "Yb": 1.0,
    "U": 2.0, "Np": 3.0, "Pu": 4.0,
}

# ---- 静态自洽的关键参数（改这里，别改 INCAR_SET_BASE）----
# EDIFF: step1 模板里的 1E-8 是为了配合 EDIFFG=-0.001 的力判据，静态自洽用不上
#        那么严。1E-7 兼顾精度与耗时；要算形成能这类小能量差可收紧到 1E-8。
#        本值会一路传到 step3 / step4（那两步不再改写 EDIFF）。
STEP2_EDIFF = "1E-7"
# ISYM: 自洽用的是【对称约化后的不可约 k 点集】，必须开对称化把电荷密度恢复回来，
#       所以默认 2。只有加外电场 / Berry 相极化这类破坏对称性的场景才需要改 0，
#       且改 0 后必须保证 KPOINTS 是完整网格（自动网格格式会由 VASP 自行处理）。
STEP2_ISYM = "2"

INCAR_REMOVE = {"EDIFFG", "POTIM", "ISIF", "IOPTCELL"}
INCAR_SET_BASE = {
    "EDIFF": STEP2_EDIFF,
    "IBRION": "-1",
    "NSW": "0",
    "ISYM": STEP2_ISYM,
    "LORBIT": "11",
    "LWAVE": ".FALSE.",
    "LCHARG": ".TRUE.",
}


def resolve_step1_dir():
    """STEP1_DIR='auto' 时，按 c -> b -> a -> 旧单目录 的顺序找最后一个有 CONTCAR 的。"""
    if STEP1_DIR != "auto":
        return STEP1_DIR
    for name in ("step1_opt", "step1_std_opt", "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt", "step1_PBE_opt"):
        if Path(name, "CONTCAR").is_file() or Path(name, "POSCAR").is_file():
            print("[..] step1 来源：%s" % name)
            return name
    sys.exit("[ERROR] 找不到任何 step1 目录（step1c/b/a_PBE_opt 或 step1_PBE_opt）。\n"
             "        请在流程父目录下运行，或把 STEP1_DIR 写死成具体目录名。")


def parse_args():
    p = argparse.ArgumentParser(description="Generate step2 consistent static inputs")
    p.add_argument("--vaspkit", default="vaspkit", help="VASPKIT executable")
    p.add_argument("--no-vaspkit", action="store_true",
                   help="Reuse step1 KPOINTS instead of regenerating it")
    p.add_argument("--kscheme", default=KSCHEME)
    p.add_argument("--kspacing", default=KSPACING)
    p.add_argument("--jobname", default=None,
                   help="Slurm job name (default: <label>_s2static)")
    return p.parse_args()


def sanitize_label(text: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return label.strip("_.-") or "material"


def read_structure_label(path: Path) -> str:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return "material"
    token = lines[0].split()[0] if lines[0].split() else "material"
    return sanitize_label(token)


def parse_incar(path):
    items = []
    with open(path) as handle:
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
        "[ERROR] step1 method is not one of the supported choices. "
        f"Detected GGA={gga or '(missing)'}, IVDW={ivdw or '(missing)'}. "
        f"Supported: {', '.join(SUPPORTED_FUNCS)}."
    )


def read_species_and_counts(path: Path):
    """从 POSCAR 读 (元素符号列表, 各元素原子数)。VASP4 无符号行时符号为 None。"""
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


def decide_magnetism(symbols, counts, step1_ispin, step1_moms):
    """按 AUTO_MAG 规则返回 (magnetic, magmom_str_or_None, note, warns)。
       magmom_str=None 且 magnetic=True 表示"原样继承 step1 的 MAGMOM"。"""
    warns = []
    if AUTO_MAG is False:
        return False, None, "AUTO_MAG=False 强制非磁", warns

    table = dict(MAG_ELEM_MOMENTS)
    table.update({k: float(v) for k, v in MAGMOM_OVERRIDE.items()})

    if MAGMOM_OVERRIDE:
        if symbols is None:
            warns.append("MAGMOM_OVERRIDE 需要 POSCAR 有元素符号行，已忽略")
        else:
            magmom = "  ".join("%d*%g" % (n, table.get(s, 0.0))
                               for s, n in zip(symbols, counts))
            return True, magmom, "MAGMOM_OVERRIDE(手动)", warns

    if step1_ispin == 2:
        if step1_moms is not None:
            mmax = max((abs(m) for m in step1_moms), default=0.0)
            if mmax <= MAG_ZERO_TOL:
                if AUTO_MAG is True:
                    warns.append("step1 磁矩已塌缩(max|m|=%.3f)但 AUTO_MAG=True，"
                                 "仍按磁性处理并继承 step1 MAGMOM" % mmax)
                    return True, None, "强制磁性(继承 step1 MAGMOM)", warns
                return False, None, ("step1 收敛磁矩全部≈0(max|m|=%.3f) —— "
                                     "已自动降级为非磁 ISPIN=1" % mmax), warns
            magmom = " ".join("%g" % round(m, 3) for m in step1_moms)
            return True, magmom, ("继承 step1 OUTCAR 收敛磁矩(max|m|=%.2f)" % mmax), warns
        warns.append("step1 是 ISPIN=2 但读不到 OUTCAR 磁矩，原样继承 step1 的 MAGMOM")
        return True, None, "磁性(继承 step1 MAGMOM)", warns

    # step1 非磁
    hits = [] if symbols is None else [s for s in symbols if table.get(s, 0.0) != 0.0]
    if hits:
        magmom = "  ".join("%d*%g" % (n, table.get(s, 0.0))
                           for s, n in zip(symbols, counts))
        warns.append("POSCAR 含磁性候选元素 %s 但 step1 是非磁跑的！本步已自动改为 "
                     "ISPIN=2 高自旋起点。注意：step1 的【几何】是按非磁弛豫的，"
                     "磁性体系建议用新版 gen_step1 重跑弛豫。" % "/".join(sorted(set(hits))))
        return True, magmom, "元素自动判定(step1 为非磁，自愈)", warns
    if AUTO_MAG is True:
        raise SystemExit("[ERROR] AUTO_MAG=True 但 step1 非磁且元素表无候选，"
                         "请用 MAGMOM_OVERRIDE 给初始磁矩")
    return False, None, "非磁 (ISPIN=1)", warns


def build_incar(src_items, remove, set_values):
    remove = {key.upper() for key in remove}
    set_values = {key.upper(): value for key, value in set_values.items()}
    body = []
    seen = set()
    for key, value in src_items:
        if key in remove or key == "SYSTEM":
            continue
        body.append((key, set_values.get(key, value)))
        seen.add(key)
    for key, value in set_values.items():
        if key != "SYSTEM" and key not in seen:
            body.append((key, value))
    lines = [f"SYSTEM = {set_values['SYSTEM']}"]
    lines.extend(f"{key:<8s} = {value}" for key, value in body)
    return "\n".join(lines) + "\n"


def render_submit(tpl_path, out_path, params):
    if not os.path.exists(tpl_path):
        raise SystemExit(f"[ERROR] Missing submit template: {tpl_path}")
    text = Path(tpl_path).read_text(encoding="utf-8")
    for key, value in params.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftover = set(re.findall(r"\{\{(\w+)\}\}", text))
    if leftover:
        raise SystemExit(
            f"[ERROR] Unfilled placeholders in {tpl_path}: {leftover}. "
            "Only {{JOBNAME}} is filled by this script; hard-code the rest "
            "directly in the template."
        )
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
def run_vaspkit_kpoints(exe, outdir, kscheme, kspacing):
    print(f"[..] Regenerating KPOINTS: 1 -> 102 -> {kscheme} -> {kspacing}")
    subprocess.run(
        [exe], input=f"1\n102\n{kscheme}\n{kspacing}\n",
        text=True, cwd=outdir, check=True,
    )


def main():
    args = parse_args()
    step1 = Path(resolve_step1_dir())
    step2 = Path(STEP2_DIR)
    if not step1.is_dir():
        sys.exit(f"[ERROR] Missing {step1}; run in the workflow parent directory")

    incar_path = step1 / INCAR_FILE
    if not incar_path.exists():
        sys.exit(f"[ERROR] Missing {incar_path}")

    struct = next((step1 / name for name in ("CONTCAR", "POSCAR")
                   if (step1 / name).exists()), None)
    if struct is None:
        sys.exit(f"[ERROR] No CONTCAR/POSCAR in {step1}")
    # v1.3：接力结构完整性校验——step1 还在跑时 CONTCAR 只写了一半，
    # 直接拷给 step2 会让 vaspkit 读文件崩（forrtl end-of-file）
    bad = validate_poscar(struct)
    if bad:
        sys.exit(f"[ERROR] {struct} 不完整：{bad}。\n"
                 "        step1 弛豫很可能还在跑（CONTCAR 写了一半）——\n"
                 "        等 tf 里 S1 变 done 再生成 S2；强行用半成品结构算静态没有意义。")

    items = parse_incar(incar_path)
    method = detect_method(items, step1 / METHOD_FILE)
    label = read_structure_label(struct)
    step2.mkdir(parents=True, exist_ok=True)   # ke-dft-cpu：STEP2_DIR 是嵌套路径

    Path(step2 / "POSCAR").write_text(
        struct.read_text(encoding="utf-8-sig"), encoding="utf-8", newline="\n"
    )
    if struct.name != "CONTCAR":
        print("[WARN] step1 CONTCAR is absent; using step1 POSCAR")

    # ---- 维度：优先继承 step1 workflow_method.txt 的 DIM=，缺失按结构判定 ----
    dim, dim_note = resolve_dim(step1 / METHOD_FILE, step2 / "POSCAR")
    require_dim(dim, ('2d', '3d'), "step2_static",
                why="静态自洽本身对分子成立，但本脚本的 KPOINTS 仍按固体网格生成；要跑 0D 请先照 band-dft-cpu 的 gen_step2_static 补 Gamma 分支")
    submit_tpl = resolve_tpl(Path.cwd(), "submit_std", dim)
    print(f"[..] Dimension: {dim.upper()} — {dim_note}")
    print(f"[..] Submit template: {submit_tpl.name}")

    potcar_src = step1 / POTCAR_FILE
    if not potcar_src.exists():
        sys.exit(f"[ERROR] Missing {potcar_src}")
    shutil.copyfile(potcar_src, step2 / POTCAR_FILE)

    if args.no_vaspkit:
        old_kpoints = step1 / "KPOINTS"
        if not old_kpoints.exists():
            sys.exit("[ERROR] --no-vaspkit was used but step1 KPOINTS is missing")
        shutil.copyfile(old_kpoints, step2 / "KPOINTS")
        print("[WARN] Reused step1 KPOINTS; regeneration is recommended after ISIF=3")
    else:
        try:
            run_vaspkit_kpoints(args.vaspkit, step2, args.kscheme, args.kspacing)
            if dim == "2d":
                changed, kz_note = force_kz1(step2 / "KPOINTS")
                print(f"[{'OK' if changed else '..'}] 2D KPOINTS vacuum-axis subdivision: {kz_note}")
        except FileNotFoundError:
            sys.exit(f"[ERROR] VASPKIT not found: {args.vaspkit}")
        except subprocess.CalledProcessError as exc:
            sys.exit(f"[ERROR] VASPKIT failed, return code={exc.returncode}")

    jobname = args.jobname or sanitize_label(f"{label}_s2static")[:80]
    render_submit(str(submit_tpl), step2 / "submit.sh", {"JOBNAME": jobname})
    sub_ov = dict(SUBMIT_OVERRIDE)
    sub_ov.update(stepconf.read_submit(stepconf.CONF_NAME))
    stepconf.apply_submit(step2 / "submit.sh", sub_ov)

    # ---- 磁性自动处理 ----
    symbols, counts = read_species_and_counts(step2 / "POSCAR")
    nions = sum(counts)
    step1_ispin = 1
    for key, value in items:
        if key == "ISPIN":
            try:
                step1_ispin = int(value.split()[0])
            except ValueError:
                pass
    step1_moms = read_magnetization(step1 / "OUTCAR", nions)
    magnetic, magmom, mag_note, mag_warns = decide_magnetism(
        symbols, counts, step1_ispin, step1_moms)

    incar_set = dict(INCAR_SET_BASE)
    incar_set["SYSTEM"] = f"{label} {method} static (step2)"
    # 并行参数按宿主机自适应：GPU 版强制 NCORE=1/KPAR=1，CPU 保持模板默认
    incar_set.update(adaptive_parallel_tags())
    incar_remove = set(INCAR_REMOVE)
    if magnetic:
        incar_set["ISPIN"] = "2"
        if magmom is not None:
            incar_set["MAGMOM"] = magmom
        # magmom=None: 原样继承 step1 INCAR 的 MAGMOM，不动
    else:
        incar_set["ISPIN"] = "1"
        incar_remove.add("MAGMOM")
        incar_remove.add("NUPDOWN")
    text = build_incar(items, incar_remove, incar_set)
    (step2 / "INCAR").write_text(text, encoding="utf-8", newline="\n")

    method_src = step1 / METHOD_FILE
    if method_src.exists():
        shutil.copyfile(method_src, step2 / METHOD_FILE)
    else:
        (step2 / METHOD_FILE).write_text(
            f"FUNC={method}\nLABEL={label}\nDIM={dim.upper()}\n",
            encoding="utf-8", newline="\n"
        )

    final = {key: value for key, value in parse_incar(step2 / "INCAR")}
    print(f"Structure : {struct} -> {step2 / 'POSCAR'}")
    print(f"Method    : {method} (GGA={final.get('GGA')}, IVDW={final.get('IVDW', 'off')})")
    print(f"Dimension : {dim.upper()} ({dim_note})")
    print(f"Magnetism : {'ON — ' + mag_note if magnetic else 'off — ' + mag_note}")
    if magnetic and final.get("MAGMOM"):
        print(f"            MAGMOM = {final['MAGMOM']}")
    for w in mag_warns:
        print(f"[WARN] {w}")
    print(f"KPOINTS   : {'reused' if args.no_vaspkit else 'regenerated from optimized structure'}")
    print(f"Job name  : {jobname}")
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", "submit.sh", METHOD_FILE):
        print(f"[{'OK' if (step2 / name).exists() else 'MISSING'}] {name}")
    print("\n[DONE] step2_PBE_static is ready (vasp_std)")


if __name__ == "__main__":
    main()
