#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step3.2_vacuum_align.py
===========================
真空能级对齐 + 全解水(HER/OER)能带匹配判据。step3(PBEsol)算完后运行。

【它解决什么】
    step3 的 band_summary.json 给的 VBM/CBM 是相对 VASP【内部参考】的，直接和
    水的氧化还原电位(-4.44 / -5.67 eV vs 真空)比没有意义。本脚本读 step3 自洽
    输出的 LOCPOT，沿真空轴做平面平均取真空能级 E_vac，把 VBM/CBM 平移到【真空
    标度】(E_vac = 0)，再和 HER/OER 线(含 pH 扫描)比对。

【前提】step3 的 INCAR 必须开了 LVHAR=.TRUE.（输出 LOCPOT）；2D 不对称体系还应
    开 LDIPOL + IDIPOL(=真空轴)让两侧真空平台等高。band_summary.json 与 LOCPOT
    必须出自【同一次 step3 SCF】，内部零点才一致(平移量才对)。

【同泛函适用】step4(HSE) 同理：它自己也开了 LVHAR，把 --band-dft-cpu-dir 指到
    step4_band_plot、--locpot 指到 step4 的 LOCPOT 即可复用本脚本。

【关键数据打印】E_vac、真空标度下 VBM/CBM、带隙、与 HER/OER 的差值(pH 扫描)、
    是否跨越(straddle) 判定，全部打到 stderr，并落盘 vacuum_align_summary.json。

【与 taskflow 对接】stdout 只输出一行 JSON；stderr 是过程日志；
    退出码 0=成功 40=错误。落盘 vacuum_align_summary.json 可作 check:plot 的
    done_marker。

用法:
    cd <材料目录>                       # 里面有 step3_PBE_WAVECAR/ 和 step3_band_plot/
    python gen_step3.2_vacuum_align.py
    python gen_step3.2_vacuum_align.py --ph 0 7          # 只看这两个 pH
    python gen_step3.2_vacuum_align.py --vac-axis c      # 手动指定真空轴(默认自动)
    # 复用给 step4(HSE):
    python gen_step3.2_vacuum_align.py \
        --scf-dir step4_HSE_band --band-dft-cpu-dir step4_band_plot --out-dir step4_vacuum

依赖: numpy, matplotlib
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

# ============================== 配置 ==============================
SCF_DIR_DEFAULT  = "step3_PBE_WAVECAR"   # 有 LOCPOT / OUTCAR 的自洽目录
BAND_DIR_DEFAULT = "step3_band_plot"     # 有 band_summary.json 的目录
OUT_DIR_DEFAULT  = "step3_vacuum"        # 输出目录

# 水的氧化还原电位 vs 真空(绝对电极电位模型, pH=0)。越负=能量越深。
# HER: H+/H2  = -4.44 + 0.059*pH      OER: O2/H2O = -5.67 + 0.059*pH
E_HER_PH0 = -4.44
E_OER_PH0 = -5.67
PH_SLOPE  = 0.059                        # eV / pH（Nernst）
DEFAULT_PH = [0.0, 7.0]

VAC_PLATEAU_FRAC = 0.15   # 取真空轴上远离 slab 的这个比例宽度作平台
FLAT_TOL   = 0.05         # eV，平台起伏 > 此值 -> 告警(可能需要 DIPOL)
SIDE_TOL   = 0.05         # eV，两侧平台高差 > 此值 -> 告警(偶极没修干净)
AXIS_MAP   = {"a": 0, "b": 1, "c": 2, "0": 0, "1": 1, "2": 2}
# =================================================================


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 读 LOCPOT（VASP 局域势，CHGCAR 同格式：头 + 网格维度 + 体数据）
# ---------------------------------------------------------------------------
def read_locpot(path: Path):
    """返回 (lattice 3x3 Ang, ngx, ngy, ngz, data[ngx,ngy,ngz])。
    LOCPOT 与 CHGCAR 格式一致，但 LOCPOT 存的是势(eV)、不乘晶胞体积。"""
    with open(path, "r", errors="ignore") as f:
        lines = f.readlines()
    scale = float(lines[1].split()[0])
    lat = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)],
                   dtype=float)
    if scale < 0:
        vol = abs(np.linalg.det(lat))
        scale = (abs(scale) / vol) ** (1.0 / 3.0)
    lat *= scale

    # 找元素计数行 -> 原子数 -> 坐标行 -> 空行 -> 网格维度行
    idx = 5
    if not lines[idx].split()[0].strip().lstrip("-").isdigit():
        idx += 1                       # 有元素符号行
    counts = [int(x) for x in lines[idx].split()]
    nat = sum(counts)
    idx += 1
    if lines[idx].strip().upper().startswith(("S",)):   # Selective dynamics
        idx += 1
    idx += 1                            # Direct/Cartesian 行
    idx += nat                          # 跳过原子坐标
    # 跳过可能的空行
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    ngx, ngy, ngz = (int(x) for x in lines[idx].split()[:3])
    idx += 1
    # 读体数据(可能有多列)
    vals = []
    need = ngx * ngy * ngz
    while idx < len(lines) and len(vals) < need:
        for tok in lines[idx].split():
            try:
                vals.append(float(tok))
            except ValueError:
                pass
        idx += 1
    if len(vals) < need:
        raise ValueError("LOCPOT 体数据不足：期望 %d，读到 %d" % (need, len(vals)))
    data = np.array(vals[:need]).reshape((ngz, ngy, ngx)).transpose(2, 1, 0)  # Fortran 序
    return lat, ngx, ngy, ngz, data


def planar_average(data, axis):
    """沿指定轴做平面平均：返回 (frac_coord[n], avg_potential[n])。"""
    other = tuple(i for i in range(3) if i != axis)
    prof = data.mean(axis=other)
    n = prof.shape[0]
    frac = (np.arange(n) + 0.5) / n
    return frac, prof


def vacuum_level(frac, prof, lat, axis):
    """取真空平台：找势最平坦的一段(远离 slab)。
    返回 (E_vac, plateau_info)。plateau_info 含平台起伏、两侧高差等自检量。"""
    n = len(prof)
    w = max(3, int(round(VAC_PLATEAU_FRAC * n)))
    # 滑动窗口找起伏最小的一段(真空区势最平)
    best_i, best_std = 0, np.inf
    for i in range(n):
        seg = np.take(prof, range(i, i + w), mode="wrap")
        s = seg.std()
        if s < best_std:
            best_std, best_i = s, i
    seg = np.take(prof, range(best_i, best_i + w), mode="wrap")
    e_vac = float(seg.mean())

    # 自检：两侧平台高差(偶极是否修干净)。取平台段两端各一小窗对比。
    q = max(2, w // 3)
    left  = float(np.take(prof, range(best_i, best_i + q), mode="wrap").mean())
    right = float(np.take(prof, range(best_i + w - q, best_i + w), mode="wrap").mean())
    d_perp = abs(np.linalg.det(lat)) / np.linalg.norm(
        np.cross(lat[(axis + 1) % 3], lat[(axis + 2) % 3]))
    info = {
        "plateau_center_frac": round((best_i + w / 2) / n % 1.0, 4),
        "plateau_width_frac": round(w / n, 4),
        "plateau_ripple_eV": round(best_std, 4),
        "plateau_side_diff_eV": round(abs(left - right), 4),
        "vac_thickness_Ang": round(d_perp, 3),
    }
    return e_vac, info


# ---------------------------------------------------------------------------
# 真空轴：优先读 POSCAR 自动判定(同 gen_step3_WAVECAR 的 detect_vacuum_axis 逻辑)
# ---------------------------------------------------------------------------
def read_poscar_lat_pos(poscar: Path):
    lines = poscar.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    scale = float(lines[1].split()[0])
    lat = np.array([[float(x) for x in lines[i].split()[:3]] for i in (2, 3, 4)],
                   dtype=float)
    if scale < 0:
        vol = abs(np.linalg.det(lat))
        scale = (abs(scale) / vol) ** (1.0 / 3.0)
    lat *= scale
    i = 5
    if not lines[i].split()[0].strip().lstrip("-").isdigit():
        i += 1
    counts = [int(x) for x in lines[i].split()]
    nat = sum(counts)
    i += 1
    if lines[i].strip().upper().startswith(("S", "s")):
        i += 1
    direct = lines[i].strip().upper().startswith(("D", "d"))
    i += 1
    pos = np.array([[float(x) for x in lines[i + k].split()[:3]] for k in range(nat)],
                   dtype=float)
    if not direct:
        pos = (pos * scale) @ np.linalg.inv(lat)
    return lat, pos % 1.0


def detect_vacuum_axis(lat, pos, vacuum_min=8.0):
    found = []
    for ax in range(3):
        h = np.linalg.norm(np.cross(lat[(ax + 1) % 3], lat[(ax + 2) % 3]))
        d_perp = abs(np.linalg.det(lat)) / h
        s = np.sort(pos[:, ax])
        gap_frac = 1.0 if len(s) == 1 else \
            np.diff(np.concatenate([s, [s[0] + 1.0]])).max()
        if gap_frac * d_perp >= vacuum_min:
            found.append(ax)
    if len(found) == 1:
        return found[0]
    return None   # 0 个(3D) 或 多个(1D/0D)：交给调用方处理


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="真空能级对齐 + 全解水能带匹配判据")
    ap.add_argument("--scf-dir", default=SCF_DIR_DEFAULT,
                    help="含 LOCPOT/POSCAR 的自洽目录(默认 %s)" % SCF_DIR_DEFAULT)
    ap.add_argument("--band-dft-cpu-dir", default=BAND_DIR_DEFAULT,
                    help="含 band_summary.json 的目录(默认 %s)" % BAND_DIR_DEFAULT)
    ap.add_argument("--out-dir", default=OUT_DIR_DEFAULT,
                    help="输出目录(默认 %s)" % OUT_DIR_DEFAULT)
    ap.add_argument("--locpot", default=None, help="LOCPOT 路径(默认 <scf-dir>/LOCPOT)")
    ap.add_argument("--vac-axis", default=None, help="真空轴 a/b/c，默认自动判定")
    ap.add_argument("--ph", type=float, nargs="*", default=None,
                    help="pH 列表(默认 0 7)")
    args = ap.parse_args()

    scf = Path(args.scf_dir).resolve()
    bnd = Path(args.band_dir).resolve()
    out = Path(args.out_dir).resolve()
    phs = args.ph if args.ph else DEFAULT_PH
    base = {"step": "vacuum_align", "scf_dir": str(scf), "band_dir": str(bnd),
            "checked_at": datetime.now().isoformat(timespec="seconds")}

    locpot = Path(args.locpot).resolve() if args.locpot else scf / "LOCPOT"
    if not locpot.exists():
        emit({**base, "status": "error",
              "reason": "缺少 LOCPOT(%s)——step3 必须开 LVHAR=.TRUE. 并跑完自洽" % locpot}, 40)
    bsum = bnd / "band_summary.json"
    if not bsum.exists():
        emit({**base, "status": "error",
              "reason": "缺少 %s——请先运行画能带脚本" % bsum}, 40)

    # 1) band_summary.json 拿 VBM/CBM(内部参考零点)
    try:
        bd = json.loads(bsum.read_text(encoding="utf-8"))
        evbm = float(bd["vbm"]["E_eV"])
        ecbm = float(bd["cbm"]["E_eV"])
        gap  = float(bd.get("gap_eV", ecbm - evbm))
        func = bd.get("functional_display", "?")
    except Exception as exc:
        emit({**base, "status": "error", "reason": "读 band_summary.json 失败: %s" % exc}, 40)

    # 2) 真空轴
    vac_axis = None
    if args.vac_axis is not None:
        vac_axis = AXIS_MAP.get(str(args.vac_axis).lower())
        if vac_axis is None:
            emit({**base, "status": "error", "reason": "--vac-axis 只能是 a/b/c"}, 40)
    else:
        poscar = scf / "POSCAR"
        if poscar.exists():
            try:
                lat_p, pos_p = read_poscar_lat_pos(poscar)
                vac_axis = detect_vacuum_axis(lat_p, pos_p)
            except Exception:
                vac_axis = None
    if vac_axis is None:
        emit({**base, "status": "error",
              "reason": "无法自动判定真空轴(可能是 3D 体相或 1D/0D)。3D 无真空能级可对齐；"
                        "2D 请用 --vac-axis 指定"}, 40)
    log("[..] 真空轴 = %s" % "abc"[vac_axis])

    # 3) 读 LOCPOT -> 平面平均 -> 真空能级
    try:
        lat, ngx, ngy, ngz, data = read_locpot(locpot)
        frac, prof = planar_average(data, vac_axis)
        e_vac, plat = vacuum_level(frac, prof, lat, vac_axis)
    except Exception as exc:
        emit({**base, "status": "error", "reason": "LOCPOT 处理失败: %s" % exc}, 40)

    warns = []
    if plat["plateau_ripple_eV"] > FLAT_TOL:
        warns.append("真空平台起伏 %.3f eV 偏大(>%.2f)：真空层可能不够厚，或需显式 DIPOL"
                     % (plat["plateau_ripple_eV"], FLAT_TOL))
    if plat["plateau_side_diff_eV"] > SIDE_TOL:
        warns.append("两侧真空平台高差 %.3f eV(>%.2f)：偶极没修干净——确认 step3 开了 "
                     "LDIPOL+IDIPOL=%d，必要时显式给 DIPOL(电荷中心)"
                     % (plat["plateau_side_diff_eV"], SIDE_TOL, vac_axis + 1))
    for w in warns:
        log("[warn] " + w)

    # 4) 平移到真空标度(E_vac = 0)
    vbm_vac = evbm - e_vac
    cbm_vac = ecbm - e_vac

    # 5) 与 HER/OER 比对(pH 扫描)。straddle 判据：CBM 高于 HER 且 VBM 低于 OER。
    ph_rows = []
    for ph in phs:
        her = E_HER_PH0 + PH_SLOPE * ph
        oer = E_OER_PH0 + PH_SLOPE * ph
        cbm_above_her = cbm_vac - her       # >0 = CBM 够高，能还原 H+
        vbm_below_oer = oer - vbm_vac       # >0 = VBM 够低，能氧化 H2O
        straddle = (cbm_above_her > 0) and (vbm_below_oer > 0)
        ph_rows.append({
            "pH": ph,
            "E_HER_eV": round(her, 3), "E_OER_eV": round(oer, 3),
            "CBM_above_HER_eV": round(cbm_above_her, 3),
            "VBM_below_OER_eV": round(vbm_below_oer, 3),
            "straddle": bool(straddle),
        })

    # ---------- 关键参数打印 ----------
    log("=" * 60)
    log("[真空对齐] 泛函=%s   真空轴=%s   E_vac=%.4f eV" % (func, "abc"[vac_axis], e_vac))
    log("[真空对齐] 真空标度(E_vac=0)下：")
    log("           VBM = %+.3f eV   CBM = %+.3f eV   Eg = %.3f eV"
        % (vbm_vac, cbm_vac, gap))
    log("[真空对齐] 平台自检：起伏=%.3f eV  两侧高差=%.3f eV  真空厚=%.1f Å"
        % (plat["plateau_ripple_eV"], plat["plateau_side_diff_eV"],
           plat["vac_thickness_Ang"]))
    log("-" * 60)
    log("  pH   HER(eV)  OER(eV)  CBM-HER  OER-VBM  straddle")
    for r in ph_rows:
        log("  %-4g %8.3f %8.3f %+8.3f %+8.3f    %s"
            % (r["pH"], r["E_HER_eV"], r["E_OER_eV"],
               r["CBM_above_HER_eV"], r["VBM_below_OER_eV"],
               "✓" if r["straddle"] else "✗"))
    log("=" * 60)
    log("[提示] 这是【单粒子】带边判据(PBEsol 隙偏小，视作下界)。真实驱动力还需扣激子"
        "束缚能(富勒烯网络 C24 可达 0.4~0.5 eV)——那一步靠 step4/TDHF。")

    # 6) 出图
    out.mkdir(exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # (a) 平面平均静电势
    fig1, ax1 = plt.subplots(figsize=(6, 4))
    x_ang = frac * plat["vac_thickness_Ang"]
    ax1.plot(x_ang, prof, color="#1f4e79", lw=1.2)
    ax1.axhline(e_vac, color="crimson", ls="--", lw=1.0, label="E$_{vac}$=%.3f eV" % e_vac)
    ax1.set_xlabel("distance along %s (Å)" % "abc"[vac_axis])
    ax1.set_ylabel("planar-avg electrostatic potential (eV)")
    ax1.set_title("%s  planar-averaged LOCPOT" % func)
    ax1.legend(fontsize=9)
    fig1.tight_layout()
    p_pot = out / "planar_potential.png"
    fig1.savefig(p_pot, dpi=200)

    # (b) 能带边 vs HER/OER 对齐图(以真空为 0；画成 vs 真空的负值，越深越下)
    fig2, ax2 = plt.subplots(figsize=(5, 5))
    ph_plot = phs[0]
    her = E_HER_PH0 + PH_SLOPE * ph_plot
    oer = E_OER_PH0 + PH_SLOPE * ph_plot
    ax2.bar(0, cbm_vac - vbm_vac, bottom=vbm_vac, width=0.5,
            color="#9ecae1", edgecolor="k", label="band-dft-cpu gap")
    ax2.axhline(her, color="tab:green", ls="--", lw=1.2,
                label="H$^+$/H$_2$ (%.2f eV)" % her)
    ax2.axhline(oer, color="tab:red", ls="--", lw=1.2,
                label="O$_2$/H$_2$O (%.2f eV)" % oer)
    ax2.text(0, cbm_vac + 0.05, "CBM %.2f" % cbm_vac, ha="center", fontsize=8)
    ax2.text(0, vbm_vac - 0.12, "VBM %.2f" % vbm_vac, ha="center", fontsize=8)
    ax2.set_xlim(-0.6, 0.6)
    ax2.set_xticks([])
    ax2.set_ylabel("Energy vs vacuum (eV)")
    ax2.set_title("%s  band-dft-cpu edges vs water (pH=%g)" % (func, ph_plot))
    ax2.legend(fontsize=8, loc="best")
    fig2.tight_layout()
    p_align = out / "band_alignment.png"
    fig2.savefig(p_align, dpi=200)

    log("[OK] %s / %s" % (p_pot.name, p_align.name))

    result = {**base, "status": "ok",
              "functional": func,
              "vac_axis": "abc"[vac_axis],
              "E_vac_eV": round(e_vac, 4),
              "vbm_vs_vacuum_eV": round(vbm_vac, 4),
              "cbm_vs_vacuum_eV": round(cbm_vac, 4),
              "gap_eV": round(gap, 4),
              "plateau_check": plat,
              "warnings": warns,
              "water_alignment": ph_rows,
              "note_exciton": ("单粒子带边判据；PBEsol 隙偏小视作下界；真实驱动力"
                               "需扣激子束缚能(step4/TDHF)"),
              "files": {"planar_potential": str(p_pot),
                        "band_alignment": str(p_align),
                        "summary": str(out / "vacuum_align_summary.json")}}
    (out / "vacuum_align_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result, 0)


if __name__ == "__main__":
    main()
