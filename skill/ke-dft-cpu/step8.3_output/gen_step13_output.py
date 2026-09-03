#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step13_output.py —— 三方电子输运对比（step8.3_output）。

run:gen 步骤：登录节点直接跑、秒级。读取
  · step8_amset/transport.json        (amset：σ/S/κ_e/迁移率，多温度多掺杂，绝对值)
  · step8.1_boltztrap/boltztrap_crta.json (BoltzTraP2 CRTA：σ/τ、S、κ_e/τ、Lorenz)
  · step8.2_dpt/dpt_result.json       (DPT：ADP 声学支迁移率，300K)
在 **300 K 附近** 并排出 σ / S / κ_e / Lorenz / 迁移率 的对比（表 + 图）。

可比性说明（写进产物里）：
  · S 和 Lorenz 与弛豫时间无关 → amset 与 BoltzTraP2 可**直接对比**（核心交叉验证）。
  · σ、κ_e：amset 是绝对值，BoltzTraP2 是 per-τ（除以弛豫时间）→ 只比**形状/趋势**。
  · 迁移率：amset(overall) 随掺杂变；DPT 是单值(仅 ADP)、随 T 按 1/T 标度 → 画成水平线。
  · 张量各向同性化：3D 取对角平均 (xx+yy+zz)/3；2D 取面内 (xx+yy)/2（剔除真空 zz），
    与 step8.1_boltztrap 一致。剔除 zz 的理由：真空方向群速度≈0，σ_zz/κ_zz≈0 是稀释项，
    而 S_zz=α_zz/σ_zz 是 0/0 病态比值——把它塞进平均会不可控地污染 S。
    2D 面内各向异性(xx≠yy)时，另出 S_xx/S_yy、σ_xx/σ_yy、κ_xx/κ_yy 分方向列，ZT 分方向算。

产物（done_marker）：comparison_300K.png + comparison_300K.csv + comparison_summary.txt
缺任一输入都不崩，能画多少画多少。
"""
import json
import math
import os
import sys
from pathlib import Path

# [SKILL_REV] 版本戳：写进 comparison_summary.txt。每次改本脚本逻辑后更新。
#   陈旧副本已咬人三次（step12/step9b 盖戳后，step13 是最后一个没盖的），
#   这里盖戳便于从产物反查到底跑的是哪份 skill 副本。
_SKILL_REV = "2026-08-31-dpt-md-klroot"

OUTDIR_NAME = "step8.3_output"
AMSET_DIR = "step8_amset"
BT2_DIR   = "step8.1_boltztrap"
DPT_DIR   = "step8.2_dpt"
TARGET_T  = 300.0
# 表里报的代表性载流子浓度（cm^-3，负=n型电子，正=p型空穴）
TARGET_N = [-1e20, -1e19, -1e18, 1e18, 1e19, 1e20]

SOMMERFELD_L = 2.44e-8   # WΩ/K²
# patch_kl_auto：晶格热导率来源。
#   "auto" = 自动读 kl-dft-cpu 技能的 step6_kappa/kappa_summary.json，取 TARGET_T 处的
#            **原始** kappa_xx_yy_zz（元胞口径）。本步 amset 的 σ/κ_e 也是元胞
#            口径，两边天然同口径，所以这里不做 c/t 重标度。
#            （要文献口径的绝对值请看 step8.1 的 NORM_2D。）
#   None   = 只用下面手填的值。
KAPPA_L_SOURCE = "auto"
# 多链探测：kl-dft-cpu → kl-mace-cpu → kl-mace-gpu（sibling 子目录，取第一个有结果的）。
KL_CHAIN_DIRS  = {
    "kl-dft-cpu":  ("step6_kappa", "step6_kappa_shengbte"),
    "kl-mace-cpu": ("step4_kappa",),
    "kl-mace-gpu": ("step4_kappa",),
}
KL_CHAIN_ORDER = ("kl-dft-cpu", "kl-mace-cpu", "kl-mace-gpu")
KL_ROOT        = None   # None=按 KL_CHAIN_ORDER 探测 sibling；填路径=只找该目录（配 KL_KAPPA_DIRS）
KL_KAPPA_DIRS  = ("step6_kappa", "step6_kappa_shengbte")   # 仅 KL_ROOT 手填时用
# 手填值（W/mK，元胞口径）。非 None 时优先于 auto。
KAPPA_L_XX_W_MK = None
KAPPA_L_YY_W_MK = None
E_C = 1.602176634e-19    # C
M0  = 9.1093837015e-31   # kg


# ---------- 通用 ----------
def _load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def _unwrap(v):
    """monty 的 {@class:array,data:[...]} → 原始 list；否则原样返回。"""
    if isinstance(v, dict) and v.get("@class") == "array":
        return v.get("data")
    return v


# ---------- 维度判定 & 张量约化 ----------
_STEP1_DIM_CANDS = ("step1_opt", "step1_std_opt",
                    "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")
ANISO_TOL = 0.05   # 面内 xx/yy 相对差 > 5% 视为各向异性 → 分方向报值


def _read_dim(cwd):
    """从 step1 的 workflow_method.txt 读 DIM=（2d/3d/0d）；读不到返回 None。
    与 step8.1_boltztrap 的判维口径一致。"""
    for name in _STEP1_DIM_CANDS:
        mf = Path(cwd) / name / "workflow_method.txt"
        if not mf.is_file():
            continue
        for ln in mf.read_text(errors="ignore").splitlines():
            if ln.strip().upper().startswith("DIM="):
                return ln.split("=", 1)[1].strip().lower()
    return None


def _diag_avg(t33, is_2d=False):
    """张量各向同性化。
    3D: (xx+yy+zz)/3。
    2D: (xx+yy)/2 —— 剔除真空方向 zz。zz 沿真空群速度 v_z≈0：对 σ/κ 而言
        分量≈0，是把正确值稀释成 2/3 的项；对 S=α/σ 而言是 0/0 的病态比值，
        污染方向和幅度都不可控，必须剔除。与 step8.1_boltztrap 保持一致。
    """
    if is_2d:
        return (t33[0][0] + t33[1][1]) / 2.0
    return (t33[0][0] + t33[1][1] + t33[2][2]) / 3.0


def _inplane_anisotropy(out):
    """S/σ/κ_e 面内 xx-yy 的最大相对差；判定是否需要分方向报值。"""
    def _reldiff(xx, yy):
        m = 0.0
        for a, b in zip(xx, yy):
            if a == a and b == b:            # 跳过 NaN
                d = max(abs(a), abs(b))
                if d > 0:
                    m = max(m, abs(a - b) / d)
        return m
    r = {"seebeck": _reldiff(out["seebeck_xx"], out["seebeck_yy"]),
         "sigma":   _reldiff(out["sigma_xx"],   out["sigma_yy"]),
         "kappa_e": _reldiff(out["kappa_e_xx"], out["kappa_e_yy"])}
    r["max"] = max(r["seebeck"], r["sigma"], r["kappa_e"])
    r["anisotropic"] = r["max"] > ANISO_TOL
    return r


def _nearest_T_index(temps, target):
    return min(range(len(temps)), key=lambda i: abs(temps[i] - target))


def _interp_loglog(x_signed, y, target_signed):
    """在同号子集里，对 |x| 做 log 插值取 target。x_signed/target_signed 同号才有效。"""
    import numpy as np
    if target_signed < 0:
        idx = [i for i in range(len(x_signed)) if x_signed[i] < 0]
    else:
        idx = [i for i in range(len(x_signed)) if x_signed[i] > 0]
    if len(idx) < 2:
        return None
    xs = np.array([math.log10(abs(x_signed[i])) for i in idx])
    ys = np.array([y[i] for i in idx])
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    xt = math.log10(abs(target_signed))
    if xt < xs.min() or xt > xs.max():
        return None      # 不外推
    # patch_logy：σ/κ_e/μ 在相邻掺杂点间能差一个数量级以上，y 线性插值会错得很远。
    # 正定且跨度 >10 倍的量改在 log10(y) 上插。S 可正可负、Lorenz 跨度小，走原路。
    if np.all(ys > 0) and (ys.max() / ys.min()) > 10.0:
        return float(10.0 ** np.interp(xt, xs, np.log10(ys)))
    return float(np.interp(xt, xs, ys))


# ---------- 读三方 ----------
def load_amset(cwd, is_2d=False):
    j = _load_json(Path(cwd) / AMSET_DIR / "transport.json")
    if not j:
        return None
    try:
        doping = _unwrap(j["doping"])
        temps = _unwrap(j["temperatures"])
        iT = _nearest_T_index(temps, TARGET_T)
        cond = _unwrap(j["conductivity"])
        seeb = _unwrap(j["seebeck"])
        kel = _unwrap(j["electronic_thermal_conductivity"])
        mob = j.get("mobility")
        mob_over = mob_adp = None
        if isinstance(mob, dict):
            mob_over = _unwrap(mob.get("overall") or mob.get("Overall"))
            mob_adp = _unwrap(mob.get("ADP") or mob.get("ACD"))  # 纯声学形变势
        out = {"T_used": temps[iT], "is_2d": bool(is_2d),
               "doping_signed": [float(x) for x in doping],
               "sigma": [], "seebeck": [], "kappa_e": [], "lorenz": [],
               "mobility": [], "mobility_adp": [],
               # 面内原始对角分量：各向异性诊断 + 分方向报值（3D 时也存着，不用而已）
               "sigma_xx": [], "sigma_yy": [],
               "seebeck_xx": [], "seebeck_yy": [],
               "kappa_e_xx": [], "kappa_e_yy": []}
        for n in range(len(doping)):
            s = _diag_avg(cond[n][iT], is_2d); se = _diag_avg(seeb[n][iT], is_2d)
            k = _diag_avg(kel[n][iT], is_2d)
            out["sigma"].append(s)
            out["seebeck"].append(se)      # μV/K（amset 单位）
            out["kappa_e"].append(k)
            out["lorenz"].append(k / (s * temps[iT]) if s > 0 else float("nan"))
            out["mobility"].append(_diag_avg(mob_over[n][iT], is_2d) if mob_over else float("nan"))
            out["mobility_adp"].append(_diag_avg(mob_adp[n][iT], is_2d) if mob_adp else float("nan"))
            out["sigma_xx"].append(cond[n][iT][0][0]);  out["sigma_yy"].append(cond[n][iT][1][1])
            out["seebeck_xx"].append(seeb[n][iT][0][0]); out["seebeck_yy"].append(seeb[n][iT][1][1])
            out["kappa_e_xx"].append(kel[n][iT][0][0]);  out["kappa_e_yy"].append(kel[n][iT][1][1])
        out["aniso"] = _inplane_anisotropy(out) if is_2d else None
        return out
    except Exception as e:
        print("[WARN] amset transport.json 解析失败：%s" % type(e).__name__)
        return None


def load_bt2(cwd):
    j = _load_json(Path(cwd) / BT2_DIR / "boltztrap_crta.json")
    if not j:
        return None
    try:
        temps = j["temperatures_K"]
        iT = _nearest_T_index(temps, TARGET_T)
        carr = j.get("carrier_conc_cm-3")
        if carr is None:
            return None
        # step8.1 修正后 carrier_conc 已是 amset 约定（负=n型电子、正=p型空穴），不再取负
        n_signed = list(carr[iT])
        res = {"T_used": temps[iT], "n_signed": n_signed,
               "seebeck": [v * 1e6 for v in j["seebeck_V_per_K"][iT]],  # V/K→µV/K
               "lorenz": j["lorenz_WOhm_per_K2"][iT],
               "sigma_over_tau": j["sigma_over_tau_S_per_m_s"][iT],
               "kappa_e_over_tau": j["kappa_e_over_tau_W_per_m_K_s"][iT]}

        def _dir(key, scale=1.0):        # patch_bt2_dir：8.1 打过 aniso 补丁才有
            a = j.get(key)
            try:
                return [v * scale for v in a[iT]] if a else None
            except (IndexError, TypeError):
                return None
        res["sigma_over_tau_xx"] = _dir("sigma_over_tau_xx")
        res["sigma_over_tau_yy"] = _dir("sigma_over_tau_yy")
        res["seebeck_xx"] = _dir("seebeck_xx_V_per_K", 1e6)
        res["seebeck_yy"] = _dir("seebeck_yy_V_per_K", 1e6)
        res["kappa_e_over_tau_xx"] = _dir("kappa_e_over_tau_xx")
        res["kappa_e_over_tau_yy"] = _dir("kappa_e_over_tau_yy")
        return res
    except Exception as e:
        print("[WARN] boltztrap_crta.json 解析失败：%s" % type(e).__name__)
        return None


def _dpt_inplane_mu(bd, r):
    """by_direction 的面内平均 μ (cm²/Vs)；缺/非 2D 回退 header mobility_cm2_Vs。

    by_direction 用 full-BZ 二次型拟合的 m_d（m_d=√(m_x·m_y)），header 的
    mobility_cm2_Vs 是 3 点抛物 m* 的旧口径，两者 μ 能差近一倍（CrS2 45.6 vs 87.5）。
    主口径一律取 by_direction。3D 时 by_direction.status 非 "ok"，自然回退。"""
    if isinstance(bd, dict) and bd.get("status") == "ok":
        vals = [bd[d]["mobility_cm2_Vs"] for d in ("x", "y")
                if isinstance(bd.get(d), dict)
                and isinstance(bd[d].get("mobility_cm2_Vs"), (int, float))]
        if vals:
            return sum(vals) / len(vals)
    return r.get("mobility_cm2_Vs")


def _dpt_m_d(bd, r):
    """by_direction 的态密度质量 m_d（full-BZ 二次型）；缺/非 2D 回退 header m_eff。"""
    if isinstance(bd, dict) and isinstance(bd.get("m_d_m0"), (int, float)):
        return bd["m_d_m0"]
    return r.get("inputs", {}).get("m_eff_m0")


def load_dpt(cwd):
    j = _load_json(Path(cwd) / DPT_DIR / "dpt_result.json")
    if not j:
        return None
    out = {"T_used": j.get("temperature_K", 300.0)}
    for r in j.get("results", []):
        carrier = r["carrier"]
        bd = r.get("by_direction")
        # [fix m*口径] 主 μ/m* 取 by_direction 的 m_d + 面内平均 μ，
        #   不再用 inputs 的 3 点抛物 m*（后者 μ 系统性偏高一倍）。
        out[carrier] = _dpt_inplane_mu(bd, r)
        out["m_" + carrier] = _dpt_m_d(bd, r)
        out["dir_" + carrier] = bd   # patch_bt2_dir
        # [C6/C7] 真空对齐的 edge_flip / vac_align / provenance
        out["E1_prov_" + r["carrier"]] = r.get("inputs", {}).get("E1_provenance", "")
    # step7b_deform_read/band_edges.json 的 edge_flip / vac_align / window_scan
    be_path = Path(cwd) / "step7b_deform_read" / "band_edges.json"
    if be_path.is_file():
        be = _load_json(be_path)
        if be:
            out["vac_align"] = be.get("vac_align")
            for c in ("electron", "hole"):
                if c in be:
                    out.setdefault("edge_flip_" + c, be[c].get("edge_flip"))
                    out.setdefault("vac_scan_" + c, be[c].get("vac_window_scan"))
    return out


def _dpt_tau_s(dpt, carrier):
    """由 DPT 反推弛豫时间 τ = μ·m*/e（μ cm²/Vs→m²/Vs, m*=m_eff·m0）。返回秒或 None。"""
    if not dpt:
        return None
    mu = dpt.get(carrier)
    meff = dpt.get("m_" + carrier)
    if not (isinstance(mu, (int, float)) and isinstance(meff, (int, float))
            and mu > 0 and meff > 0):
        return None
    return (mu * 1e-4) * (meff * M0) / E_C


# ---------- 表 ----------
# === patch_zt：ZT = S²σT/(κ_L+κ_e)，分方向优先 ===
def _zt(S_uV, sigma, kappa_e, kappa_L, T):
    if None in (S_uV, sigma, kappa_e, kappa_L):
        return None
    if any(v != v for v in (S_uV, sigma, kappa_e)):      # NaN
        return None
    denom = kappa_L + kappa_e
    if denom <= 0:
        return None
    return (S_uV * 1e-6) ** 2 * sigma * T / denom


# === patch_kl_auto：从 kl-dft-cpu 自动取 κ_L ===
_KL_CACHE = {}


def _interp_T(temps, vals, T):
    """按温度线性插值；超范围返回 None（不外推）。"""
    if not temps or len(temps) != len(vals):
        return None
    pairs = sorted(zip(temps, vals))
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if T < xs[0] - 1e-9 or T > xs[-1] + 1e-9:
        return None
    for i in range(len(xs) - 1):
        if xs[i] - 1e-9 <= T <= xs[i + 1] + 1e-9:
            if abs(xs[i + 1] - xs[i]) < 1e-12:
                return ys[i]
            w = (T - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] * (1 - w) + ys[i + 1] * w
    return ys[-1]


def _find_kl_kappa(cwd):
    """多链探测 kl 的 kappa_summary.json。返回 (src, root) 或 (None, root)。"""
    if KL_ROOT:
        root = Path(KL_ROOT)
        for d in KL_KAPPA_DIRS:
            p = root / d / "kappa_summary.json"
            if p.is_file():
                return p, root
        return None, root
    base = Path(cwd).parent
    for chain in KL_CHAIN_ORDER:
        root = base / chain
        for d in KL_CHAIN_DIRS.get(chain, ()):
            p = root / d / "kappa_summary.json"
            if p.is_file():
                return p, root
    return None, base


def resolve_kappa_L(cwd):
    """返回 (kxx, kyy, lines)。手填优先；否则读 kl 链的 kappa_summary.json。
    只取原始 kappa_xx_yy_zz（元胞口径），与 amset 的 σ/κ_e 同口径。"""
    if "v" in _KL_CACHE:
        return _KL_CACHE["v"]
    if KAPPA_L_XX_W_MK is not None or KAPPA_L_YY_W_MK is not None:
        r = (KAPPA_L_XX_W_MK, KAPPA_L_YY_W_MK,
             ["kappa_L 来源：手填（元胞口径）"])
        _KL_CACHE["v"] = r
        return r
    if not KAPPA_L_SOURCE or str(KAPPA_L_SOURCE).lower() != "auto":
        r = (None, None, ["kappa_L 未提供 —— 不出 ZT 列"])
        _KL_CACHE["v"] = r
        return r
    src, root = _find_kl_kappa(cwd)
    if src is None:
        r = (None, None, ["kappa_L 未找到：sibling kl-dft-cpu/kl-mace-cpu/kl-mace-gpu 下都没有 kappa_summary.json",
                          "  kl 链跑完了吗？或用 KL_ROOT 显式指定 kl 的材料目录"])
        _KL_CACHE["v"] = r
        return r
    j = _load_json(src) or {}
    raw, temps = j.get("kappa_xx_yy_zz"), j.get("temperatures")
    if not raw or not temps:
        r = (None, None, ["kappa_L 读取失败：%s 缺 kappa_xx_yy_zz/temperatures"
                          % src.name])
        _KL_CACHE["v"] = r
        return r
    kxx = _interp_T(temps, [x[0] for x in raw], TARGET_T)
    kyy = _interp_T(temps, [x[1] for x in raw], TARGET_T)
    lines = ["kappa_L 来源：%s（原始元胞口径，与 amset 同口径，不重标度）"
             % os.path.relpath(str(src), str(root))]
    if kxx is None or kyy is None:
        lines.append("  T=%.0f K 超出 kl-dft-cpu 的温度网格 [%.0f, %.0f] —— 不外推，不出 ZT"
                     % (TARGET_T, min(temps), max(temps)))
        r = (None, None, lines)
        _KL_CACHE["v"] = r
        return r
    lines.append("  kappa_L(%.0f K)  xx=%.4f  yy=%.4f W/mK" % (TARGET_T, kxx, kyy))
    # 元胞闸门：kl-dft-cpu 的 Lz 与 ke-dft-cpu 的 c
    rec = _load_json(Path(cwd) / AMSET_DIR / "2d_correction.json") or {}
    Lz, cA = j.get("Lz_ang"), rec.get("cell_c_A")
    if Lz and cA:
        dc = abs(float(Lz) - float(cA)) / float(cA) * 100
        tag = "一致" if dc <= 1.0 else "★不一致★"
        lines.append("  [闸门] 元胞%s：kl-dft-cpu Lz=%.4f Å vs ke-dft-cpu c=%.4f Å（差 %.3f%%）"
                     % (tag, Lz, cA, dc))
        if dc > 1.0:
            lines.append("         两边 step1 都冻结 c，同一份输入 POSCAR 不该出现"
                         "这种差异——查 step.conf 的 CELL_POLICY/VACUUM_AXIS_POLICY")
    r = (kxx, kyy, lines)
    _KL_CACHE["v"] = r
    return r


def _add_zt_columns(row):
    """按 KAPPA_L_* 补 ZT 列。amset 分方向 + 面内平均；bt2 走文献口径
    （σ=(σ/τ)·τ_DPT，κ_e=LσT）。"""
    kxx, kyy, _ = resolve_kappa_L(Path.cwd())        # patch_kl_auto
    if kxx is None and kyy is None:
        return
    for d, kl in (("xx", kxx), ("yy", kyy)):
        if kl is None:
            continue
        row["amset_ZT_%s" % d] = _zt(row.get("amset_S_%s_uV/K" % d),
                                     row.get("amset_sigma_%s_S/m" % d),
                                     row.get("amset_kappa_e_%s_W/mK" % d),
                                     kl, TARGET_T)
        row["bt2_ZT_%s" % d] = _zt(row.get("bt2_S_%s_uV/K" % d),      # patch_bt2_dir
                                   row.get("bt2_sigma_%s_S/m" % d),
                                   row.get("bt2_kappa_e_WF_%s_W/mK" % d),
                                   kl, TARGET_T)
    kl_avg = ([k for k in (kxx, kyy) if k is not None])
    kl_avg = sum(kl_avg) / len(kl_avg)
    row["amset_ZT"] = _zt(row.get("amset_S_uV/K"), row.get("amset_sigma_S/m"),
                          row.get("amset_kappa_e_W/mK"), kl_avg, TARGET_T)
    row["bt2_ZT"] = _zt(row.get("bt2_S_uV/K"), row.get("bt2_sigma_S/m"),
                        row.get("bt2_kappa_e_WF_W/mK"), kl_avg, TARGET_T)


def _dpt_tau_dir(dpt, carrier, d):
    """分方向 τ（秒）。step8.2 没打各向异性补丁时回落到各向同性 τ。"""
    bd = (dpt or {}).get("dir_" + carrier) or {}
    if bd.get("status") == "ok" and isinstance(bd.get(d), dict):
        return bd[d].get("tau_s")
    return _dpt_tau_s(dpt, carrier)


def build_table(am, bt, dpt):
    rows = []
    for nt in TARGET_N:
        typ = "n(电子)" if nt < 0 else "p(空穴)"
        row = {"carrier_conc_cm-3": nt, "type": typ}
        if am:
            row["amset_sigma_S/m"] = _interp_loglog(am["doping_signed"], am["sigma"], nt)
            row["amset_S_uV/K"] = _interp_loglog(am["doping_signed"], am["seebeck"], nt)
            row["amset_kappa_e_W/mK"] = _interp_loglog(am["doping_signed"], am["kappa_e"], nt)
            row["amset_Lorenz"] = _interp_loglog(am["doping_signed"], am["lorenz"], nt)
            row["amset_mu_cm2/Vs"] = _interp_loglog(am["doping_signed"], am["mobility"], nt)
            if any(v == v for v in am.get("mobility_adp", [])):
                row["amset_mu_ADP_cm2/Vs"] = _interp_loglog(
                    am["doping_signed"], am["mobility_adp"], nt)
            # 2D 面内各向异性：附上分方向值（面内平均会抹掉它们）
            if am.get("aniso") and am["aniso"]["anisotropic"]:
                row["amset_S_xx_uV/K"] = _interp_loglog(am["doping_signed"], am["seebeck_xx"], nt)
                row["amset_S_yy_uV/K"] = _interp_loglog(am["doping_signed"], am["seebeck_yy"], nt)
                row["amset_sigma_xx_S/m"] = _interp_loglog(am["doping_signed"], am["sigma_xx"], nt)
                row["amset_sigma_yy_S/m"] = _interp_loglog(am["doping_signed"], am["sigma_yy"], nt)
                row["amset_kappa_e_xx_W/mK"] = _interp_loglog(am["doping_signed"], am["kappa_e_xx"], nt)
                row["amset_kappa_e_yy_W/mK"] = _interp_loglog(am["doping_signed"], am["kappa_e_yy"], nt)
        if bt:
            row["bt2_S_uV/K"] = _interp_loglog(bt["n_signed"], bt["seebeck"], nt)
            row["bt2_Lorenz"] = _interp_loglog(bt["n_signed"], bt["lorenz"], nt)
            row["bt2_sigma/tau"] = _interp_loglog(bt["n_signed"], bt["sigma_over_tau"], nt)
        if dpt:
            row["dpt_mu_cm2/Vs"] = dpt.get("electron") if nt < 0 else dpt.get("hole")
        # κ_e 三方（BT2 用 DPT-τ 反推、DPT 用 WF 估）
        carrier = "electron" if nt < 0 else "hole"
        if bt and dpt:
            tau = _dpt_tau_s(dpt, carrier)
            ket = _interp_loglog(bt["n_signed"], bt["kappa_e_over_tau"], nt)
            row["bt2_kappa_e_W/mK"] = (ket * tau) if (ket is not None and tau) else None
            # patch_bt2_abs：CRTA+DPT（= 文献做法）的绝对 σ、PF 与 WF-κ_e。
            #   注意 bt2_kappa_e_W/mK 是零电流下的 κ_e（BoltzTraP2 已扣 S²σT），
            #   bt2_kappa_e_WF_W/mK 才是文献 Eq.(5) 的 κ_e = LσT，两者不是同一个量。
            sot = _interp_loglog(bt["n_signed"], bt["sigma_over_tau"], nt)
            sig = (sot * tau) if (sot is not None and tau) else None
            row["bt2_sigma_S/m"] = sig
            _s = row.get("bt2_S_uV/K")
            if sig is not None and _s is not None and _s == _s:
                row["bt2_PF_W/mK2"] = (_s * 1e-6) ** 2 * sig
                row["bt2_kappa_e_WF_W/mK"] = SOMMERFELD_L * sig * TARGET_T
            # patch_bt2_dir：每个方向用它自己的 τ_α —— 这才是文献的做法
            for _d, _dd in (("xx", "x"), ("yy", "y")):
                _sot = bt.get("sigma_over_tau_%s" % _d)
                _sbk = bt.get("seebeck_%s" % _d)
                if not _sot:
                    continue
                _td = _dpt_tau_dir(dpt, carrier, _dd)
                _sd = _interp_loglog(bt["n_signed"], _sot, nt)
                _sgd = (_sd * _td) if (_sd is not None and _td) else None
                row["bt2_sigma_%s_S/m" % _d] = _sgd
                _sv = _interp_loglog(bt["n_signed"], _sbk, nt) if _sbk else None
                row["bt2_S_%s_uV/K" % _d] = _sv
                if _sgd is not None and _sv is not None and _sv == _sv:
                    row["bt2_PF_%s_W/mK2" % _d] = (_sv * 1e-6) ** 2 * _sgd
                    row["bt2_kappa_e_WF_%s_W/mK" % _d] = SOMMERFELD_L * _sgd * TARGET_T
        if dpt:
            mu = dpt.get(carrier)
            if isinstance(mu, (int, float)) and mu > 0:
                sig = abs(nt) * 1e6 * E_C * (mu * 1e-4)      # n cm^-3→m^-3; S/m
                row["dpt_kappa_e_WF_W/mK"] = SOMMERFELD_L * sig * TARGET_T
        _add_zt_columns(row)        # patch_zt
        rows.append(row)
    return rows


def write_table(out, rows, am, bt, dpt):
    import csv
    cols = ["carrier_conc_cm-3", "type", "amset_sigma_S/m", "amset_S_uV/K",
            "amset_kappa_e_W/mK", "amset_Lorenz", "amset_mu_cm2/Vs",
            "amset_mu_ADP_cm2/Vs"]
    # 2D 面内各向异性时，把分方向列插在平均列后面
    if am and am.get("aniso") and am["aniso"]["anisotropic"]:
        cols += ["amset_S_xx_uV/K", "amset_S_yy_uV/K",
                 "amset_sigma_xx_S/m", "amset_sigma_yy_S/m",
                 "amset_kappa_e_xx_W/mK", "amset_kappa_e_yy_W/mK"]
    cols += ["bt2_S_uV/K", "bt2_Lorenz", "bt2_sigma/tau", "bt2_sigma_S/m",
             "bt2_PF_W/mK2", "bt2_kappa_e_W/mK", "bt2_kappa_e_WF_W/mK",
             "dpt_mu_cm2/Vs", "dpt_kappa_e_WF_W/mK"]
    cols += [c for c in ("bt2_S_xx_uV/K", "bt2_S_yy_uV/K",            # patch_bt2_dir
                         "bt2_sigma_xx_S/m", "bt2_sigma_yy_S/m",
                         "bt2_PF_xx_W/mK2", "bt2_PF_yy_W/mK2",
                         "bt2_kappa_e_WF_xx_W/mK", "bt2_kappa_e_WF_yy_W/mK")
             if any(c in r for r in rows)]
    if any(resolve_kappa_L(Path.cwd())[:2]):                          # patch_kl_auto
        cols += [c for c in ("amset_ZT_xx", "amset_ZT_yy", "amset_ZT",
                             "bt2_ZT_xx", "bt2_ZT_yy", "bt2_ZT")
                 if any(c in r for r in rows)]
    with open(out / "comparison_300K.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})

    def fmt(v, f="%.4g"):
        return (f % v) if isinstance(v, (int, float)) and v == v else "—"
    _red = "面内(xx+yy)/2 [2D]" if (am and am.get("is_2d")) else "对角(xx+yy+zz)/3 [3D]"
    lines = ["# 300 K 三方电子输运对比（详见 comparison_300K.csv）",
             "# skill_rev: %s" % _SKILL_REV,
             "# 有 amset=%s  BoltzTraP2=%s  DPT=%s" % (bool(am), bool(bt), bool(dpt)),
             "# 张量约化：%s" % _red,
             "# S/Lorenz 可直接比；σ/κ_e amset是绝对值、BT2是per-τ只比趋势；DPT迁移率仅ADP",
             "# [DPT μ] m* 取 full-BZ 二次型 m_d（面内平均），非 3 点抛物拟合"]
    # [C6/C7] DPT 真空对齐结局 / edge_flip / 来源
    if dpt:
        va = dpt.get("vac_align") or {}
        if va.get("status") == "skipped":
            lines.append("# [DPT 真空对齐] 跳过：%s" % va.get("reason", "未知原因"))
        elif va.get("status") == "ok":
            lines.append("# [DPT 真空对齐] 成功 (window_half=%.2f, flat_tol=%.4g eV)" % (
                va.get("window_half", 0.25), va.get("flat_tol_eV", 1e-3)))
        for c in ("electron", "hole"):
            flip = dpt.get("edge_flip_" + c)
            if flip:
                lines.append("# [WARN] %s 带边占据态翻转（形变后带序交换）：%s" % (c, "; ".join(flip)))
            prov = dpt.get("E1_prov_" + c, "")
            if "真空对齐" in prov:
                lines.append("# [DPT] %s E1 来源: %s" % (c, prov))
    if am and am.get("aniso") and am["aniso"]["anisotropic"]:
        a = am["aniso"]
        lines.append("# [各向异性] 面内 xx-yy 最大相对差 %.1f%%"
                     "（S %.1f%% / σ %.1f%% / κ_e %.1f%%）："
                     "见 CSV 的 *_xx/*_yy 列，ZT 建议分方向算"
                     % (a["max"] * 100, a["seebeck"] * 100,
                        a["sigma"] * 100, a["kappa_e"] * 100))
    lines.append("")
    hdr = "%-13s %-8s | %-10s %-9s %-9s | %-9s %-9s | %-9s" % (
        "n(cm^-3)", "type", "amsetS", "amsetL", "amsetμ", "bt2 S", "bt2 L", "dptμ")
    lines.append(hdr); lines.append("-" * len(hdr))
    for r in rows:
        lines.append("%-13.2e %-8s | %-10s %-9s %-9s | %-9s %-9s | %-9s" % (
            r["carrier_conc_cm-3"], r["type"],
            fmt(r.get("amset_S_uV/K")), fmt(r.get("amset_Lorenz")),
            fmt(r.get("amset_mu_cm2/Vs")), fmt(r.get("bt2_S_uV/K")),
            fmt(r.get("bt2_Lorenz")), fmt(r.get("dpt_mu_cm2/Vs"))))
    (out / "comparison_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 图 ----------
def make_figure(out, am, bt, dpt):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def split(sig, y):
        n = [(abs(sig[i]), y[i]) for i in range(len(sig))
             if sig[i] < 0 and y[i] == y[i]]
        p = [(abs(sig[i]), y[i]) for i in range(len(sig))
             if sig[i] > 0 and y[i] == y[i]]
        return sorted(n), sorted(p)

    fig, ax = plt.subplots(2, 3, figsize=(16.5, 9))
    # (0,0) Seebeck
    a = ax[0, 0]
    if am:
        n, p = split(am["doping_signed"], am["seebeck"])
        if n: a.plot(*zip(*[(x, abs(y)) for x, y in n]), "o-", c="C0", label="amset n")
        if p: a.plot(*zip(*[(x, abs(y)) for x, y in p]), "s-", c="C1", label="amset p")
    if bt:
        n, p = split(bt["n_signed"], bt["seebeck"])
        if n: a.plot(*zip(*[(x, abs(y)) for x, y in n]), "--", c="C0", alpha=.7, label="BT2 n")
        if p: a.plot(*zip(*[(x, abs(y)) for x, y in p]), "--", c="C1", alpha=.7, label="BT2 p")
    a.set_xscale("log"); a.set_xlabel("|carrier conc| (cm$^{-3}$)")
    a.set_ylabel("|Seebeck| (uV/K)"); a.set_title("Seebeck @300K (directly comparable)")
    a.legend(fontsize=8); a.grid(alpha=.3)
    # (0,1) Lorenz
    a = ax[0, 1]
    if am:
        n, p = split(am["doping_signed"], am["lorenz"])
        if n: a.plot(*zip(*n), "o-", c="C0", label="amset n")
        if p: a.plot(*zip(*p), "s-", c="C1", label="amset p")
    if bt:
        n, p = split(bt["n_signed"], bt["lorenz"])
        if n: a.plot(*zip(*n), "--", c="C0", alpha=.7, label="BT2 n")
        if p: a.plot(*zip(*p), "--", c="C1", alpha=.7, label="BT2 p")
    a.axhline(SOMMERFELD_L, ls=":", c="k", label="Sommerfeld 2.44e-8")
    a.set_xscale("log"); a.set_xlabel("|carrier conc| (cm$^{-3}$)")
    a.set_ylabel("Lorenz (W.Ohm/K^2)"); a.set_title("Lorenz @300K (directly comparable)")
    a.legend(fontsize=8); a.grid(alpha=.3)
    # (0,2) sigma: amset absolute + BT2 per-tau (right axis)
    a = ax[0, 2]
    if am:
        n, p = split(am["doping_signed"], am["sigma"])
        if n: a.plot(*zip(*n), "o-", c="C0", label="amset n (abs)")
        if p: a.plot(*zip(*p), "s-", c="C1", label="amset p (abs)")
        a.set_ylabel("amset sigma (S/m)")
    a.set_xscale("log"); a.set_yscale("log"); a.set_xlabel("|carrier conc| (cm$^{-3}$)")
    a.set_title("sigma @300K (amset abs; BT2 dashed=sigma/tau, shape only)")
    if bt:
        a2 = a.twinx()
        n, p = split(bt["n_signed"], bt["sigma_over_tau"])
        if n: a2.plot(*zip(*n), "--", c="C0", alpha=.6)
        if p: a2.plot(*zip(*p), "--", c="C1", alpha=.6)
        a2.set_yscale("log"); a2.set_ylabel("BT2 sigma/tau (S/m/s)")
    a.legend(fontsize=8); a.grid(alpha=.3)
    # (1,0) kappa_e 三方：amset真值 / BT2用DPT-τ反推 / DPT用WF估
    a = ax[1, 0]
    if am:
        n, p = split(am["doping_signed"], am["kappa_e"])
        if n: a.plot(*zip(*n), "o-", c="C0", label="amset n (true)")
        if p: a.plot(*zip(*p), "s-", c="C1", label="amset p (true)")
    if bt and dpt:                       # BT2 κ_e = (κ_e/τ)_BT × τ_DPT
        for carr, col, nm, neg in (("electron", "C0", "n", True),
                                   ("hole", "C1", "p", False)):
            tau = _dpt_tau_s(dpt, carr)
            if tau is None:
                continue
            pts = sorted((abs(bt["n_signed"][i]), bt["kappa_e_over_tau"][i] * tau)
                         for i in range(len(bt["n_signed"]))
                         if (bt["n_signed"][i] < 0) == neg
                         and bt["kappa_e_over_tau"][i] == bt["kappa_e_over_tau"][i])
            if pts:
                a.plot(*zip(*pts), "--", c=col, alpha=.7, label="BT2 %s (DPTτ)" % nm)
    if dpt:                              # DPT κ_e = L·σ·T (WF), σ=n e μ_DPT
        import numpy as np
        ntar = np.logspace(17, 21, 25)
        for carr, col, nm in (("electron", "C0", "n"), ("hole", "C1", "p")):
            mu = dpt.get(carr)
            if isinstance(mu, (int, float)) and mu > 0:
                sig = ntar * 1e6 * E_C * (mu * 1e-4)
                a.plot(ntar, SOMMERFELD_L * sig * 300.0, ":", c=col, alpha=.6,
                       label="DPT %s (WF)" % nm)
    a.set_xscale("log"); a.set_yscale("log"); a.set_xlabel("|carrier conc| (cm$^{-3}$)")
    a.set_ylabel("kappa_e (W/m/K)"); a.set_title("kappa_e @300K (3-way, see notes)")
    a.legend(fontsize=7); a.grid(alpha=.3)
    # (1,1) mobility: amset total(实线) + amset-ADP(点划,与DPT同为纯声学) + DPT(虚线水平)
    a = ax[1, 1]
    if am:
        n, p = split(am["doping_signed"], am["mobility"])
        if n: a.plot(*zip(*n), "o-", c="C0", label="amset n (total)")
        if p: a.plot(*zip(*p), "s-", c="C1", label="amset p (total)")
        if any(v == v for v in am.get("mobility_adp", [])):   # 有 ADP 分量才画
            na, pa = split(am["doping_signed"], am["mobility_adp"])
            if na: a.plot(*zip(*na), "-.", c="C0", alpha=.8, label="amset n (ADP-only)")
            if pa: a.plot(*zip(*pa), "-.", c="C1", alpha=.8, label="amset p (ADP-only)")
    if dpt:
        if isinstance(dpt.get("electron"), (int, float)):
            a.axhline(dpt["electron"], ls="--", c="C0", label="DPT n (ADP,300K)")
        if isinstance(dpt.get("hole"), (int, float)):
            a.axhline(dpt["hole"], ls="--", c="C1", label="DPT p (ADP,300K)")
    a.set_xscale("log"); a.set_yscale("log"); a.set_xlabel("|carrier conc| (cm$^{-3}$)")
    a.set_ylabel("mobility (cm^2/Vs)"); a.set_title("Mobility @300K (amset vs DPT-ADP)")
    a.legend(fontsize=8); a.grid(alpha=.3)
    # (1,2) notes / provenance
    a = ax[1, 2]; a.axis("off")
    notes = (
        "Mobility comparison key:\n"
        "  DPT is ADP-only (acoustic).\n"
        "  Compare DPT vs amset ADP-only\n"
        "  (dash-dot), NOT vs amset total.\n"
        "  amset total = ADP+IMP+POP.\n"
        "  If total << ADP-only, IMP/POP\n"
        "  suppress mobility (esp. polar).\n"
        "\n"
        "kappa_e provenance:\n"
        "  amset : true (full scattering).\n"
        "  BT2   : (kappa_e/tau)*tau_DPT.\n"
        "  DPT   : WF, L*sigma*T (rough).\n"
        "\n"
        "Caveat: 2D POP/dielectric use 3D\n"
        "Frohlich -> amset total may be\n"
        "over-suppressed for polar 2D.\n"
        "S & Lorenz (tau-free) are the\n"
        "clean amset-vs-BT2 check."
    )
    a.text(0.0, 0.98, notes, va="top", ha="left", fontsize=8.5,
           family="monospace", transform=a.transAxes)

    fig.suptitle("Electronic transport comparison @ ~300 K "
                 "(amset / BoltzTraP2-CRTA / DPT)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out / "comparison_300K.png", dpi=130)
    plt.close(fig)


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)
    dim = _read_dim(cwd)
    is_2d = (dim == "2d")
    print("[..] 维度 DIM=%s → amset 张量约化 %s"
          % (dim or "未知(按3D处理)",
             "面内(xx+yy)/2" if is_2d else "对角(xx+yy+zz)/3"))
    am, bt, dpt = load_amset(cwd, is_2d=is_2d), load_bt2(cwd), load_dpt(cwd)
    if not any([am, bt, dpt]):
        sys.exit("[ERROR] step8/8.1/8.2 三个结果都读不到——先把它们跑出来再做对比。")
    if am and am.get("aniso") and am["aniso"]["anisotropic"]:
        a = am["aniso"]
        print("[WARN] 2D 面内各向异性：S/σ/κ_e 的 xx-yy 最大相对差 %.1f%% (>%.0f%%)。"
              % (a["max"] * 100, ANISO_TOL * 100))
        print("       面内平均会抹掉方向信息——CSV 已附 *_xx/*_yy 分方向列，ZT 建议分方向算。")
    got = [n for n, v in (("amset", am), ("BoltzTraP2", bt), ("DPT", dpt)) if v]
    print("[..] 读到：%s；在 %.0f K 附近对比" % ("、".join(got), TARGET_T))
    rows = build_table(am, bt, dpt)
    write_table(out, rows, am, bt, dpt)
    try:
        make_figure(out, am, bt, dpt)
    except Exception as e:
        print("[WARN] 画图失败（%s）——表已生成，图跳过" % type(e).__name__)
    for _l in resolve_kappa_L(cwd)[2]:                # patch_kl_auto
        print("[..] " + _l)
    print("[DONE] %s：comparison_300K.png / .csv / summary.txt 已生成" % OUTDIR_NAME)


if __name__ == "__main__":
    main()