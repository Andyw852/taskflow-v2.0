#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mol_common.py — gen_step1_PBE_opt.py 的 0D（孤立分子/团簇）分支  v2

设计原则与主脚本一致：**本模块不写死任何 VASP 参数**。
ISIF / IBRION / NSW / EDIFFG / ISYM / LREAL / KPAR / LASPH / LDIPOL …
全部放在 incar_0d.tpl 里由项目自己定；本模块只负责三件"算出来才知道"的事：

    {{DIPOL}}   偶极修正的参考点 = 结构几何中心的分数坐标
    {{ISPIN}}   由 POTCAR 的 ZVAL 数出总价电子，奇数必须开自旋
    {{MAGMOM}}  按元素给初始磁矩（客体原子取主脚本的 MAG_ELEM_MOMENTS）

再加上主脚本原有的 SYSTEM / ENCUT / GGA / VDW_LINE / JOBNAME。
模板里没写的占位符自动跳过（沿用主脚本 render 的"有才填"语义）。

行为开关全部走 step.conf 的 [params]，不改脚本：

    MOL_KPOINTS       gamma | vaspkit       (默认 gamma)
    MOL_ISPIN         auto | 1 | 2          (默认 auto：价电子奇偶 + 磁性元素)
    MOL_MOMENT        客体原子默认初始磁矩   (默认 1.0)
    MOL_DIPOL         auto | none           (默认 auto，算几何中心)
    MOL_ENCUT_FLOOR   序列 ENCUT 下限，0=不干预 (默认 0)
    MOL_ALLOW_3D_TPL  true | false          (默认 false，见下)

主脚本 gen_step1_PBE_opt.py 需要的改动只有三处：

  1) import mol_common
  2) main() 里 POSCAR 存在性检查之后：
         if mol_common.is_molecule(cwd / "POSCAR", VACUUM_MIN, DIMENSION):
             mol_common.generate(cwd, globals())
             return
  3) validate_user_config() 里把 "0d" 加进允许值：
         if str(DIMENSION).lower() not in ("auto", "0d", "2d", "3d"):
     否则 DIMENSION="0d" 强制指定时会被主脚本判成非法而退出。

模板：项目 templates 里放 incar_0d.tpl + submit_std_0d.tpl（与 _2d/_3d 同约定）。
找不到 incar_0d.tpl 默认直接报错——3D 体相模板里的 ISIF=3 会把分子的真空盒压塌，
静默回退比报错更危险。确实想借用，就在 step.conf 写 MOL_ALLOW_3D_TPL = true。
"""

import re
import sys
from pathlib import Path

import numpy as np

# ---- 只有"本模块自己的行为默认值"，没有 VASP 参数 ----
CONF_SPEC_MOL = {
    "MOL_KPOINTS":      ("gamma", "str"),
    "MOL_ISPIN":        ("auto", "str"),
    "MOL_MOMENT":       ("1.0", "str"),
    "MOL_DIPOL":        ("auto", "str"),
    "MOL_ENCUT_FLOOR":  ("0", "str"),
    "MOL_ALLOW_3D_TPL": ("false", "str"),
}
OUTDIR_NAME = "step1_PBE_opt"      # 兜底值；调用方（relax_common）传了 OUTDIR_SINGLE 就用它
                                   # —— 各技能的 step1 目录名不同（band-dft-cpu: step1_PBE_opt，
                                   #    elastic-dft-cpu/ke-dft-cpu/kl-dft-cpu: step1_std_opt），必须跟着走
HOST_ELEMENT = "C"                 # 骨架元素，初始磁矩给 0；其余算客体


def _conf(G):
    """MOL_* 参数：从调用方已经解析好的 STEP_PARAMS 里取（见 relax_common
    的 load_step_params）。本模块【不】自己 stepconf.load —— stepconf 对未声明
    的键会报错，各自解析会互相踩（FUNC 对 mol_common 是未知键，反之亦然）。"""
    vals = {k: v[0] for k, v in CONF_SPEC_MOL.items()}
    got = (G.get("STEP_PARAMS") or {}) if hasattr(G, "get") else {}
    for k in vals:
        if got.get(k) is not None:
            vals[k] = str(got[k])
    return vals


def _as_bool(s):
    return str(s).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------
# POSCAR 几何
# ---------------------------------------------------------------------
def _read_poscar(poscar: Path):
    lines = Path(poscar).read_text(encoding="utf-8-sig").splitlines()
    scale = float(lines[1].split()[0])
    latt = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)]) * scale
    line6 = lines[5].split()
    if line6 and line6[0].lstrip("-").isdigit():
        counts, i = [int(x) for x in line6], 6
    else:
        counts, i = [int(x) for x in lines[6].split()], 7
    if lines[i].strip()[:1].upper() == "S":
        i += 1
    direct = lines[i].strip()[:1].upper() == "D"
    i += 1
    n = sum(counts)
    pos = np.array([[float(x) for x in lines[i + k].split()[:3]] for k in range(n)])
    frac = pos if direct else pos @ np.linalg.inv(latt)
    return latt, counts, frac


def vacuum_gaps(poscar: Path):
    """沿三个晶轴的最大真空间隙 (Å)：周期性地找分数坐标里的最大空隙。"""
    latt, _, frac = _read_poscar(poscar)
    frac = frac % 1.0
    gaps = []
    for ax in range(3):
        f = np.sort(frac[:, ax])
        d = np.diff(np.append(f, f[0] + 1.0))
        gaps.append(float(d.max() * np.linalg.norm(latt[ax])))
    return gaps


def structure_center_frac(poscar: Path):
    """结构几何中心的分数坐标，作为 DIPOL 参考点。"""
    _, _, frac = _read_poscar(poscar)
    return frac.mean(axis=0) % 1.0


def is_molecule(poscar: Path, vacuum_min: float, dimension_setting: str = "auto"):
    """>=2 个方向有真空 -> 0D。DIMENSION='0d' 强制走这里；'2d'/'3d' 一定不走。"""
    mode = str(dimension_setting).lower()
    if mode == "0d":
        return True
    if mode in ("2d", "3d"):
        return False
    return sum(1 for g in vacuum_gaps(poscar) if g >= vacuum_min) >= 2


# ---------------------------------------------------------------------
# POTCAR：价电子总数（判奇偶）
# ---------------------------------------------------------------------
def valence_electrons(potcar: Path, counts):
    zs = [float(m.group(1)) for m in
          (re.search(r"ZVAL\s*=\s*([\d.]+)", l)
           for l in Path(potcar).read_text(errors="ignore").splitlines()) if m]
    if len(zs) != len(counts):
        return None
    return sum(z * n for z, n in zip(zs, counts))


def decide_spin(symbols, counts, nelect, mag_table, conf):
    """返回 (ispin, magmom_str_or_None, note)。magmom 用 per-species 压缩写法。"""
    want = str(conf["MOL_ISPIN"]).strip().lower()
    odd = nelect is not None and int(round(nelect)) % 2 == 1
    guest_hits = [s for s in (symbols or []) if s != HOST_ELEMENT
                  and mag_table.get(s, 0.0) != 0.0]

    if want == "1":
        return 1, None, "step.conf 指定 MOL_ISPIN=1"
    if want == "2":
        why = "step.conf 指定 MOL_ISPIN=2"
    elif odd:
        why = "总价电子 %d 为奇数，必须开自旋" % round(nelect)
    elif guest_hits:
        why = "含磁性候选客体 %s" % "/".join(sorted(set(guest_hits)))
    elif nelect is None:
        why = "读不到价电子数（无 POTCAR），保守开自旋"
    else:
        return 1, None, "总价电子 %d 为偶数且客体非磁性候选" % round(nelect)

    try:
        default_moment = float(conf["MOL_MOMENT"])
    except ValueError:
        default_moment = 1.0
    moments = []
    for s, n in zip(symbols or [], counts):
        m = 0.0 if s == HOST_ELEMENT else (mag_table.get(s) or default_moment)
        moments.append("%d*%g" % (n, m))
    return 2, "  ".join(moments), why


# ---------------------------------------------------------------------
# 模板 / KPOINTS
# ---------------------------------------------------------------------
def _find_tpl(cwd: Path, name: str):
    for base in (cwd, cwd.parent):
        p = base / name
        if p.is_file():
            return p
    return None


def _pick_tpl(cwd: Path, stem: str, conf, G):
    tpl = _find_tpl(cwd, "%s_0d.tpl" % stem)
    if tpl:
        return tpl
    if _as_bool(conf["MOL_ALLOW_3D_TPL"]):
        print("[WARN] 没找到 %s_0d.tpl，按 step.conf 的 MOL_ALLOW_3D_TPL 回退到 3D 模板 —— "
              "请自行确认里面是固定胞 ISIF=2、无 IOPTCELL、KPAR=1" % stem)
        return G["resolve_tpl"](cwd, stem, "3d")
    sys.exit("[ERROR] 0D 体系需要 %s_0d.tpl（放进项目 templates，与 _2d/_3d 同一约定）。\n"
             "        不给专用模板而借用 3D 模板很危险：ISIF=3 会把分子的真空盒压塌。\n"
             "        确实要借用，请在 step.conf 写 MOL_ALLOW_3D_TPL = true。" % stem)


def write_kpoints(outdir: Path, cwd: Path, conf, G):
    """kpoints_0d.tpl 优先；否则按 MOL_KPOINTS 走 Γ 或 VASPKIT。"""
    tpl = _find_tpl(cwd, "kpoints_0d.tpl")
    if tpl:
        (outdir / "KPOINTS").write_text(tpl.read_text(encoding="utf-8"),
                                        encoding="utf-8", newline="\n")
        print("[OK] KPOINTS（来自 %s）" % tpl.name)
        return
    mode = str(conf["MOL_KPOINTS"]).strip().lower()
    if mode == "vaspkit":
        G["run_vaspkit_kpoints"](G["VASPKIT_EXE"], outdir, G["KSCHEME"], G["KSPACING"])
        print("[OK] KPOINTS（VASPKIT 102，step.conf MOL_KPOINTS=vaspkit）")
        return
    (outdir / "KPOINTS").write_text(
        "Gamma only (0D molecule)\n0\nGamma\n1 1 1\n0 0 0\n",
        encoding="utf-8", newline="\n")
    print("[OK] KPOINTS（Γ only）—— 分子的能带是平的，多取 k 点只是翻倍机时；"
          "想改回 VASPKIT 就在 step.conf 写 MOL_KPOINTS = vaspkit，"
          "或放一个 kpoints_0d.tpl")


# ---------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------
def drop_empty_tags(incar_path: Path):
    """删掉值为空的标签行（例如 ISPIN=1 时的 MAGMOM=、MOL_DIPOL=none 时的 DIPOL=）。
    VASP 遇到空值会直接罢工，所以宁可整行删掉。"""
    keep, dropped = [], []
    for ln in Path(incar_path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Za-z_]+)\s*=\s*(?:[#!].*)?$", ln)
        if m:
            dropped.append(m.group(1))
            continue
        keep.append(ln)
    if dropped:
        Path(incar_path).write_text("\n".join(keep) + "\n",
                                    encoding="utf-8", newline="\n")
    return dropped


def generate(cwd: Path, G):
    """0D 分支。G = 主脚本的 globals()，直接复用其函数与配置。"""
    if "--stage" in sys.argv[1:]:
        print("[WARN] 0D 没有三段式弛豫（分子固定胞，只有一段），--stage 被忽略")

    conf = _conf(G)
    outdir_name = (G.get("OUTDIR_SINGLE") if hasattr(G, "get") else None) or OUTDIR_NAME
    gaps = vacuum_gaps(cwd / "POSCAR")
    print("[..] 维度：0D — 各晶轴真空间隙 a=%.1f b=%.1f c=%.1f Å（>=2 个方向有真空）"
          % tuple(gaps))
    print("[SKIP] 原胞检查（分子没有可约化的平移对称性）")

    incar_tpl = _pick_tpl(cwd, "incar", conf, G)
    submit_tpl = _pick_tpl(cwd, "submit_std", conf, G)
    print("[..] 模板：%s + %s" % (incar_tpl.name, submit_tpl.name))

    func, func_src = G["resolve_func"](incar_tpl, outdir_name)
    G["FUNC"] = func                     # 让主脚本的 build_params/validate 用同一个值
    G["validate_user_config"]()
    print("[..] 泛函：%s（来源：%s）" % (func, func_src))

    label, formula = G["read_poscar_identity"](cwd / "POSCAR")
    params = G["build_params"](label)
    outdir = cwd / outdir_name
    outdir.mkdir(exist_ok=True)
    print("[..] 结构标签：%s   化学式：%s" % (label, formula))
    print("[..] 输出目录：%s" % outdir)

    (outdir / "POSCAR").write_text((cwd / "POSCAR").read_text(encoding="utf-8-sig"),
                                   encoding="utf-8", newline="\n")
    print("[OK] POSCAR")

    G["render"](submit_tpl, outdir / "submit.sh", params)
    # 覆盖优先级：step.conf [submit] > 脚本硬编码 SUBMIT_OVERRIDE
    sub_ov = dict(G["SUBMIT_OVERRIDE"])
    sub_ov.update(G.get("STEP_SUBMIT") or {})
    G["stepconf"].apply_submit(outdir / "submit.sh", sub_ov)

    # KPOINTS / POTCAR：POTCAR 仍由 VASPKIT 出，与 2D/3D 完全一致
    write_kpoints(outdir, cwd, conf, G)
    have_potcar = (outdir / "POTCAR").exists()
    if G["RUN_VASPKIT"]:
        try:
            G["run_vaspkit_potcar"](G["VASPKIT_EXE"], outdir)
            have_potcar = (outdir / "POTCAR").exists()
        except FileNotFoundError:
            sys.exit("[ERROR] 找不到 VASPKIT：%s" % G["VASPKIT_EXE"])
    else:
        print("[SKIP] RUN_VASPKIT=False，未生成 POTCAR")

    # ENCUT：完全沿用主脚本规则（MANUAL_ENCUT > POTCAR 自动）
    if G["MANUAL_ENCUT"] is not None:
        encut = int(G["MANUAL_ENCUT"])
        print("[..] 使用手动 ENCUT = %d eV" % encut)
    elif have_potcar:
        encut = G["encut_from_potcar"](outdir / "POTCAR", G["ENCUT_FACTOR"])
    else:
        encut = int(G["FALLBACK_ENCUT"])
        print("[WARN] 没有 POTCAR，ENCUT 暂用兜底值 %d eV" % encut)
    try:
        floor = int(float(conf["MOL_ENCUT_FLOOR"]))
    except ValueError:
        floor = 0
    if floor and encut < floor:
        print("[..] ENCUT %d 低于 step.conf 的 MOL_ENCUT_FLOOR=%d，抬到下限" % (encut, floor))
        encut = floor
    params["ENCUT"] = str(encut)

    # ---- 只有"算出来才知道"的三个量：ISPIN / MAGMOM / DIPOL ----
    symbols, counts = G["read_species_and_counts"](outdir / "POSCAR")
    nelect = valence_electrons(outdir / "POTCAR", counts) if have_potcar else None
    ispin, magmom, spin_note = decide_spin(symbols, counts, nelect,
                                           G["MAG_ELEM_MOMENTS"], conf)
    params["ISPIN"] = str(ispin)
    params["MAGMOM"] = magmom if magmom else ""
    if str(conf["MOL_DIPOL"]).strip().lower() == "none":
        params["DIPOL"] = ""
    else:
        params["DIPOL"] = "%.4f %.4f %.4f" % tuple(structure_center_frac(outdir / "POSCAR"))

    tpl_text = incar_tpl.read_text(encoding="utf-8")
    for key, why in (("ISPIN", "自旋"), ("MAGMOM", "初始磁矩"), ("DIPOL", "偶极参考点")):
        if "{{%s}}" % key not in tpl_text and params[key]:
            print("[WARN] %s 里没有 {{%s}} 占位符，算出的%s（%s = %s）不会写进 INCAR"
                  % (incar_tpl.name, key, why, key, params[key]))

    G["render"](incar_tpl, outdir / "INCAR", params)
    dropped = drop_empty_tags(outdir / "INCAR")
    if dropped:
        print("[..] 空值标签已删除：%s" % ", ".join(dropped))
    G["validate_generated_incar"](outdir / "INCAR")
    print("[OK] INCAR 泛函检查通过")
    print("[..] 自旋：ISPIN=%d — %s" % (ispin, spin_note))
    if magmom:
        print("     MAGMOM = %s" % magmom)
    if params["DIPOL"]:
        print("[..] DIPOL = %s（结构几何中心，供模板里的 LDIPOL/IDIPOL 用）" % params["DIPOL"])

    # LMAXMIX / DFT+U：沿用主脚本的判定函数，行为与 2D/3D 一致
    lmaxmix, lmm_note = G["decide_lmaxmix"](symbols)
    G["apply_lmaxmix_to_incar"](outdir / "INCAR", lmaxmix, lmm_note)
    print("[..] LMAXMIX = %d — %s" % (lmaxmix, lmm_note))

    use_u, ldau_lines, u_note = G["decide_u"](symbols)
    G["apply_u_to_incar"](outdir / "INCAR", use_u, ldau_lines, u_note)
    print("[..] DFT+U：%s — %s" % ("ON" if use_u else "off", u_note))
    if use_u:
        print("     提醒：序列里只有部分元素在 U 表里时，加 U 与不加 U 的总能不可比；"
              "整批筛选建议把 AUTO_U 关掉或全序列统一。")

    G["write_method_file"](outdir / G["METHOD_FILE"], label, formula, None,
                           mag_line="MAG=%s" % ("magnetic" if ispin == 2 else "nonmag"),
                           dim_line="DIM=0D")
    with open(outdir / G["METHOD_FILE"], "a", encoding="utf-8") as fh:
        fh.write("ENCUT=%d\n" % encut)
        fh.write("NELECT=%s\n" % ("%d" % round(nelect) if nelect else "unknown"))
    print("[OK] %s" % G["METHOD_FILE"])

    print("\n文件检查：")
    for name in ("POSCAR", "INCAR", "submit.sh", "KPOINTS", "POTCAR", G["METHOD_FILE"]):
        print("[%s] %s" % ("OK" if (outdir / name).exists() else "MISSING", name))
    print("[..] 0D 只有一段弛豫（模板里应为固定胞 ISIF=2），跑完直接进 step2")
    print("[DONE] %s 已生成（0D 分子模式），可提交" % outdir_name)
