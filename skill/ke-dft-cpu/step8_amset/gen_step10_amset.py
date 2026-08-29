#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step8_amset.py —— 写 settings.yaml → amset run → σ/S/κ_e（step8_amset）。

汇集前面所有产物，写 amset 的 settings.yaml，提交到计算节点跑 amset run。
输入软链：
  wavefunction.h5   ← step4_wave
  deformation.h5    ← step7b_deform_read
介电常数从 step5_dielect/OUTCAR 解析，带隙从带隙段或配置读，弹性从 step6_elastic。
产出目录：step8_amset/，产物 transport.json（判据看 thermal_conductivity）。
"""
import glob
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf  # noqa: E402
try:
    import ke_common as kc
    _HAS_KC = True
except Exception:
    _HAS_KC = False

# =========================== 可改参数区 ===========================
OUTDIR_NAME = "step8_amset"
WAVE_DIR    = "step4_wave"
READ_DIR    = "step7b_deform_read"
DIELECT_DIR = "step5_dielect"
# patch_ke_dag：跟上 v1.9 的目录重命名。HSE 优先，其次 PBE，最后兼容老目录。
BANDGAP_PLOT_CANDS = [
    "step2_bandgap/step2.3_hse_plot",
    "step2_bandgap/step2.2_pbe_plot",
    "step4_band_plot",
]
STEP_LABEL  = "S8_kappa"
AMSET_CMD   = ('amset run >> amset.log 2>&1 && cp -f "$(ls -t transport_*.json 2>/dev/null | head -1)" transport.json && ls -l transport.json')
# --- 输运设置（可改）---
DOPING      = "-1e21:-1e17:5, 1e17:1e21:5"   # n 型 + p 型各 5 点（对数均布）cm^-3
TEMPERATURES = "100:900:9"            # 100,200,...,900 K，每 100 K 一个点
SCATTERING  = ["ADP", "IMP", "POP"]   # 形变势声学 + 电离杂质 + 极性光学 "ADP", "IMP", "POP"
MANUAL_BANDGAP = None                 # None=自动读；或写数值(eV) 覆盖 scissor
# patch_require_bandgap：半导体输运没有 scissor 的结果没有意义——
#   读不到带隙直接停步，不再静默用 PBE 带隙跑完。金属体系才设 False。
REQUIRE_BANDGAP = True
# patch_interp_factor：AMSET 的收敛判据在**插值后**的网格上，别吃默认值 5。
#   设 None 则不写这一行（回到 AMSET 默认）。加大前先做收敛测试。
INTERPOLATION_FACTOR = 10
# --- 弹性常数来源（amset run 的 ACD 散射需要）---
#   MANUAL_ELASTIC 填了就用它，否则从 ELASTIC_DIR/OUTCAR 自动解析（kBar→GPa）。
#   直接填：单个数（各向同性近似，GPa），或 6x6 列表（完整 Cij，GPa）。
MANUAL_ELASTIC = None
ELASTIC_DIR = "step6_elastic"
# --- patch_2d_amset：2D 修正 ---------------------------------------------
# TWO_D_MODE: "auto" = 读 step1 的 workflow_method.txt 的 DIM=；"on"/"off" 强制。
TWO_D_MODE = "auto"
# LAYER_THICKNESS: 层的有效厚度（Å）。三种取值：
#   "vdw"  （默认）自动 = 原子核 z 跨度 + 两侧最外层元素各一个范德华半径。
#          这是 2D 文献做 h/d0 重标度时的通行取法，确定、可复现、可引用。
#   "span" 只用原子核 z 跨度。偏小 -> C 偏大 -> 迁移率偏高，一般别用。
#   数值    手填（Å）。已知对应体相层间距时优先用这个。
# 无论哪种，最终用的 c/t/取法/倍数都会落盘到 step8_amset/2d_correction.json。
LAYER_THICKNESS = "vdw"
# 2D 时给 settings.yaml 写 free_carrier_screening: true
FREE_CARRIER_SCREENING_2D = True
# patch_2d_dielec：2D 介电取面内 (εx+εy)/2 并扣真空 ε^m=1+(L/t)(ε_sup-1)
# （文献标准做法；L/t 复用弹性同款 c/t）。设 False 则用原始三对角平均。
TWO_D_DIELECTRIC_VACUUM = True
# 找结构的候选目录（按序取第一个有 POSCAR/CONTCAR 的）
STRUCT_CANDS = ["step3_uniform", "step7_deform", "step1_opt", "step1_std_opt"]
_STEP1_METHOD_CANDS = ["step1_opt", "step1_std_opt",
                       "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt"]
# =================================================================


def read_dielectric(dielect_dir: Path):
    """从 DFPT OUTCAR 读 ε∞（电子）与 ε₀（静态）对角平均。"""
    oc = dielect_dir / "OUTCAR"
    if not oc.is_file():
        return None, None
    txt = oc.read_text(errors="ignore")
    def grab(tag):
        # 取 tag 之后第一块 3x3，对角平均
        i = txt.rfind(tag)
        if i < 0:
            return None
        rows = []
        for ln in txt[i:].splitlines()[1:]:
            nums = re.findall(r"-?\d+\.\d+", ln)
            if len(nums) >= 3:
                rows.append([float(x) for x in nums[:3]])
            if len(rows) == 3:
                break
        if len(rows) < 3:
            return None
        return round((rows[0][0] + rows[1][1] + rows[2][2]) / 3.0, 4)
    eps_inf = grab("MACROSCOPIC STATIC DIELECTRIC TENSOR (including local field effects in DFT)")
    eps_0 = grab("MACROSCOPIC STATIC DIELECTRIC TENSOR IONIC CONTRIBUTION")
    # ε₀ = 电子 + 离子
    if eps_inf is not None and eps_0 is not None:
        eps_static = round(eps_inf + eps_0, 4)
        # patch_dielec_guard：ε_ionic<0 / ε_static<=0 非物理（多为 Γ 近零声学模
        # 污染 DFPT 离子介电）——弃离子项、退回 ε∞ 兜底并告警
        if eps_0 < 0 or eps_static <= 0:
            print("[WARN] eps_ionic=%.2f eps_static=%.2f 非物理，"
                  "多因 Gamma 近零声学模污染；弃离子项改用 eps_inf=%.2f 兜底"
                  % (eps_0, eps_static, eps_inf))
            eps_static = eps_inf
    else:
        eps_static = eps_inf
    return eps_inf, eps_static


def read_elastic(cwd: Path):
    """弹性常数来源：MANUAL_ELASTIC 优先，否则从 step6_elastic/OUTCAR 解析。
    返回 amset settings.yaml 用的值：单标量(GPa) 或 6x6 列表(GPa)，读不到返回 None。"""
    if MANUAL_ELASTIC is not None:
        return MANUAL_ELASTIC
    oc = cwd / ELASTIC_DIR / "OUTCAR"
    if not oc.is_file():
        return None
    txt = oc.read_text(errors="ignore")
    i = txt.rfind("TOTAL ELASTIC MODULI")
    if i < 0:
        return None
    rows, labels = [], ("XX", "YY", "ZZ", "XY", "YZ", "ZX")
    for ln in txt[i:].splitlines():
        p = ln.split()
        if p and p[0] in labels and len(p) >= 7:
            try:
                rows.append([float(x) for x in p[1:7]])
            except ValueError:
                pass
        if len(rows) == 6:
            break
    if len(rows) != 6:
        return None
    # VASP 输出 kBar，amset 要 GPa
    return [[round(v / 10.0, 3) for v in r] for r in rows]


def read_bandgap(cwd: Path):
    if MANUAL_BANDGAP is not None:
        return float(MANUAL_BANDGAP)
    import json
    bs = None
    for _c in BANDGAP_PLOT_CANDS:
        _p = cwd / _c / "band_summary.json"
        if _p.is_file():
            bs = _p
            print("[..] 带隙来源：%s" % _c)
            break
    if bs is not None:
        try:
            d = json.loads(bs.read_text())
            # patch_bandgap_key：band_summary.json 写的键是 gap_eV，原列表全对不上
            for k in ("gap_eV", "band_gap", "bandgap", "gap", "Egap"):
                if k in d:
                    _g = float(d[k])
                    if _g <= 0:
                        print("[WARN] band_summary 判为金属（gap=%.4f eV）——不写 bandgap"
                              % _g)
                        return None
                    print("[OK] 带隙 %.4f eV（键 %s）" % (_g, k))
                    return _g
        except Exception:
            pass
    # 退回项目配置
    for cand in (cwd / "project_setting" / "setting.yaml",):
        if cand.is_file():
            for ln in cand.read_text(errors="ignore").splitlines():
                m = re.match(r"\s*bandgap\s*:\s*([\d.]+)", ln)
                if m:
                    return float(m.group(1))
    return None


def link(out: Path, src: Path, name: str):
    if not src.is_file():
        sys.exit("[ERROR] 缺 %s：%s（前置步骤没完成？）" % (name, src))
    dst = out / name
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.symlink_to(os.path.relpath(src, out))
    print("[OK] 软链 %s" % name)


# --------------------------------------------------------------------------
# patch_2d_amset：2D 几何与弹性常数重标度
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# patch_2d_thickness：范德华半径表与 vdW 层厚
# --------------------------------------------------------------------------
# Bondi(1964) + Alvarez(2013) 常用值，单位 Å。故意内置而不用 pymatgen：
# gen 脚本在登录节点用系统 python 跑，ke_common 的设计就是不引入 pymatgen。
# patch_vdw_shared：单一真源在 skill/_common/vdw_radii.py，kl-dft-cpu 读同一份。
#   登录节点若拿不到公共池，回落最小内置表（值与公共池一致，只保证不崩）。
try:
    from vdw_radii import VDW_FALLBACK, VDW_RADII        # noqa: F401
except ImportError:
    VDW_RADII = {
        "H": 1.20, "Li": 1.82, "Be": 1.53, "B": 1.92, "C": 1.70, "N": 1.55,
        "O": 1.52, "F": 1.47, "Na": 2.27, "Mg": 1.73, "Al": 1.84, "Si": 2.10,
        "P": 1.80, "S": 1.80, "Cl": 1.75, "K": 2.75, "Ca": 2.31, "Ti": 2.11,
        "V": 2.07, "Cr": 2.06, "Mn": 2.05, "Fe": 2.04, "Co": 2.00, "Ni": 1.97,
        "Cu": 1.96, "Zn": 2.01, "Ga": 1.87, "Ge": 2.11, "As": 1.85, "Se": 1.90,
        "Br": 1.85, "Mo": 2.17, "Ru": 2.13, "Rh": 2.10, "Pd": 2.10, "Ag": 2.11,
        "Cd": 2.18, "In": 1.93, "Sn": 2.17, "Sb": 2.06, "Te": 2.06, "I": 1.98,
        "W": 2.18, "Pt": 2.13, "Au": 2.14, "Hg": 2.23, "Tl": 1.96, "Pb": 2.02,
        "Bi": 2.07,
    }
    VDW_FALLBACK = 2.00
    print("[WARN] 没找到 _common/vdw_radii.py，用内置最小表")


def _read_poscar_species_z(path: Path):
    """返回 [(元素符号, z 坐标 Å), ...] 和 c 轴长度。解析失败返回 (None, None)。"""
    try:
        lines = path.read_text(encoding="utf-8-sig",
                               errors="ignore").splitlines()
        scale = float(lines[1].split()[0])
        vecs = [[float(x) * scale for x in lines[i].split()[:3]] for i in (2, 3, 4)]
        _axb = (vecs[0][1]*vecs[1][2]-vecs[0][2]*vecs[1][1],  # patch_2d_height
                vecs[0][2]*vecs[1][0]-vecs[0][0]*vecs[1][2],
                vecs[0][0]*vecs[1][1]-vecs[0][1]*vecs[1][0])
        _A = (_axb[0]**2 + _axb[1]**2 + _axb[2]**2) ** 0.5
        c_len = (abs(vecs[2][0]*_axb[0]+vecs[2][1]*_axb[1]+vecs[2][2]*_axb[2]) / _A
                 if _A > 1e-9 else
                 (vecs[2][0]**2 + vecs[2][1]**2 + vecs[2][2]**2) ** 0.5)
        row6 = lines[5].split()
        if row6 and row6[0].lstrip("-").isdigit():
            return None, c_len              # VASP4 无元素符号行，认不出元素
        symbols = row6
        counts = [int(x) for x in lines[6].split()]
        start = 8
        mode = lines[7].strip().lower()
        if mode[:1] == "s":                 # Selective dynamics
            mode = lines[8].strip().lower()
            start = 9
        direct = mode[:1] in ("d", "")
        species = []
        for sym, n in zip(symbols, counts):
            species += [sym] * n
        out = []
        for sym, ln in zip(species, lines[start:start + len(species)]):
            p = ln.split()
            if len(p) < 3:
                continue
            z = float(p[2])
            out.append((sym, z * c_len if direct else z))
        return (out or None), c_len
    except Exception as e:                  # noqa: BLE001
        print("[WARN] 解析 %s 的元素/坐标失败：%s" % (path, e))
        return None, None


def vdw_thickness(cwd: Path):
    """t_vdW = (z_max + r_vdW[最上层元素]) - (z_min - r_vdW[最下层元素])。

    返回 (t, 说明字典)；认不出元素时返回 (None, 说明)。
    """
    for d in STRUCT_CANDS:
        for fn in ("CONTCAR", "POSCAR"):
            p = cwd / d / fn
            if not p.is_file():
                continue
            az, c_len = _read_poscar_species_z(p)
            if not az:
                continue
            top = max(az, key=lambda x: x[1])
            bot = min(az, key=lambda x: x[1])
            miss = [s for s in (top[0], bot[0]) if s not in VDW_RADII]
            if miss:
                print("[WARN] 范德华半径表里没有 %s，用 %.2f Å 兜底"
                      % ("/".join(miss), VDW_FALLBACK))
            r_top = VDW_RADII.get(top[0], VDW_FALLBACK)
            r_bot = VDW_RADII.get(bot[0], VDW_FALLBACK)
            t = (top[1] + r_top) - (bot[1] - r_bot)
            info = {
                "source_file": "%s/%s" % (d, fn),
                "z_span_A": round(top[1] - bot[1], 4),
                "top_species": top[0], "top_vdw_radius_A": r_top,
                "bottom_species": bot[0], "bottom_vdw_radius_A": r_bot,
                "vdw_radii_source": "Bondi 1964 / Alvarez 2013",
                "thickness_A": round(t, 4),
            }
            return round(t, 4), info
    return None, {"error": "找不到可解析元素符号的 POSCAR/CONTCAR"}


def write_2d_record(out: Path, record):
    import json
    (out / "2d_correction.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    print("[OK] 2d_correction.json：修正依据已落盘（写论文直接引用）")


def read_dim(cwd: Path):
    """从 step1 的 workflow_method.txt 读 DIM=（'2d'/'3d'），读不到返回 None。"""
    for name in _STEP1_METHOD_CANDS:
        mf = cwd / name / "workflow_method.txt"
        if not mf.is_file():
            continue
        for ln in mf.read_text(errors="ignore").splitlines():
            if ln.strip().upper().startswith("DIM="):
                v = ln.split("=", 1)[1].strip().lower()
                return v if v in ("2d", "3d", "0d") else None
        return None
    return None


def _read_poscar_cz(path: Path):
    """从 POSCAR 读 (c 轴长度 Å, 原子 z 向跨度 Å)。解析不了返回 (None, None)。

    只处理真空沿第 3 个晶格矢量、且 c 轴基本正交于 ab 面的常规 slab —— 这正是
    本流程强制要求的布局（dim_common 检测到真空不在 c 轴会直接退出）。
    """
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
        scale = float(lines[1].split()[0])
        vecs = [[float(x) * scale for x in lines[i].split()[:3]] for i in (2, 3, 4)]
        _axb = (vecs[0][1]*vecs[1][2]-vecs[0][2]*vecs[1][1],  # patch_2d_height
                vecs[0][2]*vecs[1][0]-vecs[0][0]*vecs[1][2],
                vecs[0][0]*vecs[1][1]-vecs[0][1]*vecs[1][0])
        _A = (_axb[0]**2 + _axb[1]**2 + _axb[2]**2) ** 0.5
        c_len = (abs(vecs[2][0]*_axb[0]+vecs[2][1]*_axb[1]+vecs[2][2]*_axb[2]) / _A
                 if _A > 1e-9 else
                 (vecs[2][0]**2 + vecs[2][1]**2 + vecs[2][2]**2) ** 0.5)
        row6 = lines[5].split()
        if row6 and row6[0].lstrip("-").isdigit():
            counts = [int(x) for x in row6]
            start = 7
        else:
            counts = [int(x) for x in lines[6].split()]
            start = 8
        n = sum(counts)
        mode = lines[start - 1].strip().lower()
        while mode[:1] in ("s",):                      # Selective dynamics
            mode = lines[start].strip().lower()
            start += 1
        direct = mode[:1] in ("d", "")
        zs = []
        for ln in lines[start:start + n]:
            p = ln.split()
            if len(p) < 3:
                continue
            z = float(p[2])
            zs.append(z * c_len if direct else z)
        if not zs:
            return c_len, None
        return c_len, max(zs) - min(zs)
    except Exception as e:                              # noqa: BLE001
        print("[WARN] 解析 %s 失败（%s），无法自动定 2D 几何" % (path, e))
        return None, None


def get_slab_geometry(cwd: Path):
    """返回 (c 轴长度 Å, 原子 z 跨度 Å)；都找不到返回 (None, None)。"""
    for d in STRUCT_CANDS:
        for fn in ("CONTCAR", "POSCAR"):
            p = cwd / d / fn
            if p.is_file():
                c_len, span = _read_poscar_cz(p)
                if c_len:
                    print("[..] 2D 几何来源：%s/%s" % (d, fn))
                    return c_len, span
    return None, None


def rescale_elastic_2d(elastic, factor):
    """把弹性常数整体乘 factor（= c/t）。标量和 6x6 都支持。"""
    if elastic is None:
        return None
    if isinstance(elastic, (int, float)):
        return round(elastic * factor, 4)
    return [[round(v * factor, 4) for v in row] for row in elastic]


def _doping_endpoints(spec):
    """从 DOPING 串里抠出出现过的浓度数值（cm^-3），用于打印面浓度换算。"""
    vals = []
    for tok in re.split(r"[,\s]+", str(spec)):
        if not tok:
            continue
        for part in tok.split(":"):
            try:
                v = float(part)
            except ValueError:
                continue
            if abs(v) >= 1e10:          # 过滤掉 ":5" 这种点数
                vals.append(v)
    return vals


def apply_2d_corrections(cwd: Path, elastic):
    """返回 (is_2d, elastic_修正后, c_len)。3D 或判定不出时原样返回。"""
    mode = str(TWO_D_MODE).lower()
    if mode == "off":
        return False, elastic, None
    if mode == "auto":
        dim = read_dim(cwd)
        if dim != "2d":
            return False, elastic, None
    elif mode != "on":
        sys.exit("[ERROR] TWO_D_MODE=%r 无效，只允许 auto / on / off" % TWO_D_MODE)

    c_len, span = get_slab_geometry(cwd)
    if c_len is None:
        print("[WARN] 2D 体系但读不到结构，弹性常数未做 c/t 修正——"
              "结果的绝对值不可用。请手填 MANUAL_ELASTIC。")
        return True, elastic, None

    # patch_2d_thickness：层厚三种取法，结果连同依据一起落盘
    mode_t = LAYER_THICKNESS
    t_info = {}
    if isinstance(mode_t, str) and mode_t.lower() == "vdw":
        t, t_info = vdw_thickness(cwd)
        t_info["method"] = "vdw"
        if t is None:
            print("[WARN] 认不出元素（VASP4 格式 POSCAR？），vdW 层厚失败，"
                  "回退到原子 z 跨度 %s Å" % span)
            t, t_info = span, {"method": "span_fallback",
                               "z_span_A": span}
        else:
            print("[OK] vdW 层厚：z 跨度 %.3f Å + %s(%.2f) + %s(%.2f) = %.3f Å"
                  % (t_info["z_span_A"], t_info["top_species"],
                     t_info["top_vdw_radius_A"], t_info["bottom_species"],
                     t_info["bottom_vdw_radius_A"], t))
    elif isinstance(mode_t, str) and mode_t.lower() == "span":
        t, t_info = span, {"method": "span", "z_span_A": span}
        print("[WARN] LAYER_THICKNESS='span'：只用核-核跨度 %s Å，"
              "偏小 -> C 偏大 -> 迁移率偏高" % t)
    elif mode_t is None:
        t, t_info = span, {"method": "span_legacy", "z_span_A": span}
        print("[WARN] LAYER_THICKNESS=None：用核-核跨度 %s Å。"
              "建议改成 \"vdw\" 或手填。" % t)
    else:
        t, t_info = mode_t, {"method": "manual"}
        print("[OK] LAYER_THICKNESS 手填 %s Å" % t)

    if t is None or float(t) <= 0.1:
        print("[WARN] 2D 体系但定不出层厚，弹性常数未做 c/t 修正。"
              "请手填 LAYER_THICKNESS。")
        return True, elastic, c_len
    t = float(t)
    if t <= 0 or t >= c_len:
        sys.exit("[ERROR] LAYER_THICKNESS=%s Å 不合理（c=%.3f Å）" % (t, c_len))

    factor = c_len / t
    new_elastic = rescale_elastic_2d(elastic, factor)
    if elastic is None:
        print("[WARN] 2D：没读到弹性常数，c/t=%.3f 的修正无从施加" % factor)
    else:
        print("[OK] 2D 弹性常数重标度：c=%.3f Å / t=%.3f Å -> ×%.3f"
              % (c_len, t, factor))
    # patch_2d_thickness：把全部依据落盘，写论文直接引用
    t_info["thickness_used_A"] = round(t, 4)
    write_2d_record(cwd / OUTDIR_NAME, {
        "cell_c_A": round(c_len, 4),
        "layer_thickness": t_info,
        "elastic_rescale_factor_c_over_t": round(factor, 4),
        "elastic_constant_raw_GPa": elastic,
        "elastic_constant_rescaled_GPa": new_elastic,
        "areal_density_factor_cm": c_len * 1e-8,
        "areal_density_note": "n_2D [cm^-2] = n_3D [cm^-3] x cell_c [cm]",
        "free_carrier_screening": bool(FREE_CARRIER_SCREENING_2D),
        "limitation": ("POP 用三维 Fröhlich 形式；二维极性材料的 Fröhlich "
                       "耦合 q 依赖不同，属定性偏差。结果只可横向比较，"
                       "绝对值不可信。可发表的迁移率请用 Perturbo / EPW。"),
    })
    return True, new_elastic, c_len


# === patch_amset_doping：settings.yaml 要数值列表，不能是 "a:b:n" 冒号语法 ===
def _seg_expand(a, b, n, log):
    import math
    n = int(round(float(n)))
    if n <= 1:
        return [float(b)]
    if log:  # 对数均布，保号（负=n型电子，正=p型空穴）
        s = -1.0 if a < 0 else 1.0
        la, lb = math.log10(abs(a)), math.log10(abs(b))
        return [s * 10 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def expand_spec(spec, log):
    """把 "a:b:n, c:d:m" 范围串或裸数值展开成 float 列表。已是数值则原样收下。"""
    out = []
    for seg in str(spec).split(","):
        seg = seg.strip()
        if not seg:
            continue
        p = seg.split(":")
        if len(p) == 3:
            out += _seg_expand(float(p[0]), float(p[1]), p[2], log)
        elif len(p) == 1:
            out.append(float(p[0]))
        else:
            sys.exit("[ERROR] 无法解析范围段 %r（应为 start:stop:count 或单个数）" % seg)
    return out



# === patch_amset_pop：DFPT OUTCAR 的 Γ 声子频率 -> pop_frequency（THz）===
def read_pop_frequency(dielect_dir):
    """取 IBRION=8 OUTCAR 里最高光学声子频率(THz)近似 pop_frequency；虚频(f/i=)
    与读不到时返回 None（此时上层自动跳过 POP）。这是"最高光学支"近似，够跑通、
    可横向比较；要发表精度请用 amset phonon-frequency 做介电加权有效频率。"""
    oc = Path(dielect_dir) / "OUTCAR"
    if not oc.is_file():
        return None
    freqs = []
    for m in re.finditer(r"^\s*\d+\s+f\s*=\s*([0-9.]+)\s*THz",
                         oc.read_text(errors="ignore"), re.M):
        try:
            freqs.append(float(m.group(1)))
        except ValueError:
            pass
    return round(max(freqs), 4) if freqs else None


def _is_nonpolar():
    """单元素体系=非极性：无极性光学声子、离子介电≈0。从 POSCAR 元素行(第6行)判断。"""
    try:
        syms = (Path.cwd() / "POSCAR").read_text(errors="ignore").splitlines()[5].split()
        if syms and not any(ch.isdigit() for ch in syms[0]):
            return len(set(syms)) == 1
    except (OSError, IndexError):
        pass
    return False


def _amset_scatterers():
    """SCATTERING 含 POP 但非极性 / 拿不到 pop_frequency 时自动去掉 POP。"""
    if "POP" in SCATTERING and _is_nonpolar():
        print("[WARN] 单元素非极性体系无极性光学声子——跳过 POP 散射")
        return [s for s in SCATTERING if s != "POP"]
    if "POP" in SCATTERING and read_pop_frequency(Path.cwd() / DIELECT_DIR) is None:
        print("[WARN] 未解析到 pop_frequency（DFPT OUTCAR 无声子频率？）——"
              "本次跳过 POP 极性光学散射；纯共价体系影响很小，极性体系请补声子数据")
        return [s for s in SCATTERING if s != "POP"]
    return list(SCATTERING)



# === patch_2d_dielec：2D 面内介电 + 扣真空 ===
def _grab_diag3(txt, tag):
    """取 tag 后第一块 3x3 的对角 [xx, yy, zz]；取不到返回 None。"""
    i = txt.rfind(tag)
    if i < 0:
        return None
    rows = []
    for ln in txt[i:].splitlines()[1:]:
        nums = re.findall(r"-?\d+\.\d+", ln)
        if len(nums) >= 3:
            rows.append([float(x) for x in nums[:3]])
        if len(rows) == 3:
            break
    return [rows[0][0], rows[1][1], rows[2][2]] if len(rows) == 3 else None


def _dielectric_2d_inplane(dielect_dir, out_dir, eps_inf_fb, eps_static_fb):
    """2D 面内介电 (εx+εy)/2 + 扣真空 ε^m = 1 + (L/t)(ε_sup − 1)。
    L/t 读 2d_correction.json 的 elastic_rescale_factor_c_over_t（与弹性同款）。
    任一步失败/离子介电为负/非极性 → 安全退回原值或仅电子项。"""
    import json as _json
    try:
        rec = _json.loads((Path(out_dir) / "2d_correction.json").read_text())
        factor = float(rec["elastic_rescale_factor_c_over_t"])
    except (OSError, KeyError, ValueError, TypeError):
        print("[WARN] 2D 介电扣真空：读不到 c/t（2d_correction.json）——保持原始三对角平均")
        return eps_inf_fb, eps_static_fb
    try:
        txt = (Path(dielect_dir) / "OUTCAR").read_text(errors="ignore")
    except OSError:
        return eps_inf_fb, eps_static_fb
    inf = _grab_diag3(txt, "MACROSCOPIC STATIC DIELECTRIC TENSOR (including local field effects in DFT)")
    ion = _grab_diag3(txt, "MACROSCOPIC STATIC DIELECTRIC TENSOR IONIC CONTRIBUTION")
    if inf is None:
        return eps_inf_fb, eps_static_fb
    inf_ip = (inf[0] + inf[1]) / 2.0                 # 面内 ε∞
    if ion is not None and not _is_nonpolar():
        stat_ip = ((inf[0] + ion[0]) + (inf[1] + ion[1])) / 2.0
        ion_ip = (ion[0] + ion[1]) / 2.0
        if ion_ip < 0 or stat_ip <= 0:               # 近零声学模污染 → 弃离子项
            stat_ip = inf_ip
    else:                                            # 非极性/无离子项：static=ε∞
        stat_ip = inf_ip
    corr_inf = round(1 + factor * (inf_ip - 1), 4)
    corr_stat = round(1 + factor * (stat_ip - 1), 4)
    print("[OK] 2D 面内介电扣真空(×L/t=%.3f)：eps_inf %.3f->%.3f  eps_static %.3f->%.3f"
          % (factor, inf_ip, corr_inf, stat_ip, corr_stat))
    return corr_inf, corr_stat



def write_settings(out: Path, eps_inf, eps_static, gap, elastic,
                   is_2d=False, c_len=None):
    lines = ["# amset settings.yaml（gen_step10 自动生成，可手改后重跑本步）"]
    if is_2d:
        # patch_2d_amset：把 2D 的三件事和一条局限写进文件头，
        # 免得几个月后拿着 transport.json 忘了这是 slab 模型跑出来的。
        lines += [
            "# ===== 2D slab 模型 =====",
            "# 1) elastic_constant 已按 c/t 重标度（还原成层材料的等效三维值）",
            "# 2) free_carrier_screening 已打开（否则 POP 主导时迁移率对浓度不响应）",
            "# 3) 下面的 doping 是体浓度 cm^-3；面浓度 n_2D = n_3D × c",
        ]
        if c_len:
            lines.append("#    本体系 c = %.4f Å = %.4e cm" % (c_len, c_len * 1e-8))
            for v in _doping_endpoints(DOPING):
                lines.append("#      %.3e cm^-3  ->  %.3e cm^-2"
                             % (v, v * c_len * 1e-8))
        lines += [
            "# 【局限】POP 用的是三维 Fröhlich 形式。二维极性材料的 Fröhlich",
            "#   耦合 q 依赖不同，这是定性偏差而非标定误差：结果只可横向比较，",
            "#   绝对值不可信。需要可发表的迁移率请用 Perturbo / EPW。",
            "# ========================",
        ]
    _dop = expand_spec(DOPING, log=True)        # doping 对数均布
    _tmp = expand_spec(TEMPERATURES, log=False)  # 温度线性
    lines += ["doping: [%s]" % ", ".join("%.6e" % v for v in _dop),
              "temperatures: [%s]" % ", ".join("%g" % v for v in _tmp),
              "scattering_type: [%s]" % ", ".join(_amset_scatterers()),
              "deformation_potential: deformation.h5"]
    if INTERPOLATION_FACTOR:     # patch_interp_factor
        lines.append("interpolation_factor: %d" % int(INTERPOLATION_FACTOR))
    _popf = read_pop_frequency(Path.cwd() / DIELECT_DIR)
    if _popf is not None and "POP" in SCATTERING:
        lines.append("pop_frequency: %s" % _popf)
    if eps_inf is not None:
        lines.append("high_frequency_dielectric: %s" % eps_inf)
    # patch_nonpolar_dielec：单元素=非极性，离子介电物理上≈0 → 静态介电=ε∞
    if _is_nonpolar() and eps_inf is not None:
        eps_static = eps_inf
    if eps_static is not None:
        lines.append("static_dielectric: %s" % eps_static)
    if gap is not None:
        lines.append("bandgap: %s" % gap)
    if elastic is not None:
        if isinstance(elastic, (int, float)):
            lines.append("elastic_constant: %s" % elastic)   # 各向同性标量
        else:                                                # 6x6 Cij
            lines.append("elastic_constant:")
            for row in elastic:
                lines.append("  - [%s]" % ", ".join("%g" % v for v in row))
    else:
        lines.append("# elastic_constant: 未读到——step6_elastic 没算完，或手填 "
                     "MANUAL_ELASTIC。ACD 散射需要它。")
    if is_2d and FREE_CARRIER_SCREENING_2D:
        lines.append("free_carrier_screening: true")
    (out / "settings.yaml").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8", newline="\n")
    ela = ("标量%s" % elastic if isinstance(elastic, (int, float))
           else ("6x6" if elastic else "无"))
    print("[OK] settings.yaml：ε∞=%s ε₀=%s gap=%s 弹性=%s"
          % (eps_inf, eps_static, gap, ela))


# patch_dim_guard：本步不跑 VASP、也不解析结构，所以没有 dim 变量可用。
# 直接从 step1 的 workflow_method.txt 读 DIM=，0D 就带原因退出，
# 免得 -f 强推时抛一句看不懂的"缺 xxx.h5"。
_STEP1_CANDS = ("step1_opt", "step1_std_opt",
                "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")


def _guard_not_0d(cwd, step_name, why):
    from pathlib import Path as _P
    for name in _STEP1_CANDS:
        mf = _P(cwd) / name / "workflow_method.txt"
        if not mf.is_file():
            continue
        for ln in mf.read_text(errors="ignore").splitlines():
            if ln.strip().upper().startswith("DIM="):
                dim = ln.split("=", 1)[1].strip().lower()
                if dim == "0d":
                    sys.exit("[ERROR] %s 不支持 0D 体系。\n"
                             "        原因：%s\n"
                             "        支持的维度：2D, 3D\n"
                             "        若判定有误，检查 %s 的 DIM=。"
                             % (step_name, why, mf))
                return
        return


def main():
    cwd = Path.cwd()
    _guard_not_0d(cwd, "step8_amset",
                  "载流子输运建立在能带色散和布里渊区积分上，孤立分子两者都没有")
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)
    link(out, cwd / WAVE_DIR / "wavefunction.h5", "wavefunction.h5")
    link(out, cwd / READ_DIR / "deformation.h5", "deformation.h5")
    # patch_amset_vasprun：amset run 还需要密网格 vasprun.xml 拿能带色散
    _vr = next((cwd / _d / "vasprun.xml"
                for _d in ("step3_uniform", "step4_wave")
                if (cwd / _d / "vasprun.xml").is_file()), None)
    if _vr is None:
        sys.exit("[ERROR] 找不到 vasprun.xml（step3_uniform / step4_wave 没跑完？）")
    link(out, _vr, "vasprun.xml")
    eps_inf, eps_static = read_dielectric(cwd / DIELECT_DIR)
    gap = read_bandgap(cwd)
    elastic = read_elastic(cwd)
    # patch_2d_amset：2D 时重标度弹性常数并记下 c，供面浓度换算
    is_2d, elastic, c_len = apply_2d_corrections(cwd, elastic)
    if is_2d and c_len and TWO_D_DIELECTRIC_VACUUM:  # patch_2d_dielec
        eps_inf, eps_static = _dielectric_2d_inplane(
            cwd / DIELECT_DIR, out, eps_inf, eps_static)
    if gap is None:
        _msg = ("没读到带隙——settings.yaml 将不写 bandgap，AMSET 会退回 "
                "step3_uniform 的裸 DFT(PBE) 带隙。PBE 带隙偏小会让 300 K 双极导通"
                "占主导，表现为：p 型 Seebeck 变负、σ 随掺杂非单调、Lorenz 数是 "
                "Sommerfeld 的数倍。这种结果不可用。\n"
                "        排查顺序：\n"
                "          1) step2_bandgap/step2.3_hse_plot/band_summary.json 在不在？"
                "里面 gap_eV 是多少？\n"
                "          2) 带隙段关掉了的话，在 project_setting/setting.yaml 写 "
                "bandgap: <eV>\n"
                "          3) 或直接在本脚本顶部写死 MANUAL_BANDGAP = <eV>\n"
                "          4) 确属金属/半金属，把 REQUIRE_BANDGAP 设为 False")
        if REQUIRE_BANDGAP:      # patch_require_bandgap
            sys.exit("[ERROR] %s" % _msg)
        print("[WARN] %s" % _msg)
    if elastic is None:
        print("[WARN] 没读到弹性常数——step6_elastic 没算完，或手填 MANUAL_ELASTIC。"
              "ACD 声学散射需要它。")
    write_settings(out, eps_inf, eps_static, gap, elastic,
                   is_2d=is_2d, c_len=c_len)

    # tf 把 submit_amset.tpl 与本脚本一起推到 gen 运行目录，但按原名推、不会改成
    # submit.sh；本步自己把它渲染成 out/submit.sh（维度步靠各自 gen 的 render，这里同理）。
    here = Path(__file__).resolve().parent
    tpl = next((p for p in (here / "submit_amset.tpl", cwd / "submit_amset.tpl")
                if p.is_file()), None)
    if tpl is None:
        sys.exit("[ERROR] 找不到 submit_amset.tpl（gen_need 里要有它，且应随 gen 脚本一起推送）")
    submit = out / "submit.sh"
    jobname = ("%s-ke-dft-cpu-%s" % (cwd.name, STEP_LABEL)) if not _HAS_KC \
        else kc.new_jobname(cwd, STEP_LABEL)
    text = tpl.read_text(encoding="utf-8")
    text = text.replace("{{JOBNAME}}", jobname).replace("{{AMSET_CMD}}", AMSET_CMD)
    submit.write_text(text, encoding="utf-8", newline="\n")
    stepconf.apply_submit(submit, stepconf.read_submit(stepconf.CONF_NAME))
    print("[DONE] %s：settings.yaml + 软链就绪，提交后 amset run 产出 transport.json"
          % OUTDIR_NAME)


if __name__ == "__main__":
    main()
