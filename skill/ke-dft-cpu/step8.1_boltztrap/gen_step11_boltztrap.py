#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step11_boltztrap.py —— BoltzTraP2 CRTA 电子输运（step8.1_boltztrap）。

run:gen 步骤：登录节点直接跑、不提交 SLURM，秒~分钟级。
读 step3_uniform 的密网格 vasprun.xml，用 BoltzTraP2 在常数弛豫时间近似(CRTA)下
算 σ/τ、Seebeck、κ_e/τ、功率因子/τ、以及洛伦兹数 L（τ 在 L 里抵消，可直接报）。
目的：给 step8 的 amset 结果做**廉价交叉验证/对标文献**（多数 2D 输运论文用 BoltzTraP）。

【不需要新的 DFT 计算】——只读 step3_uniform/vasprun.xml。
【2D/3D 通用】——自动判维；2D 额外给出 c/t（复用 step8_amset 的 2d_correction.json）
                 用于把"超胞体积口径"换算成面内薄片口径。

产出（done_marker）：本步目录下的 boltztrap_crta.json（+ 人读的 summary.txt）。

依赖：BoltzTraP2、pymatgen（tf 运行环境里需可 import）。缺了会给出清晰安装提示，
      不会抛难懂的异常。
"""
import json
import os
import sys
from pathlib import Path

# =========================== 可改参数区 ===========================
OUTDIR_NAME = "step8.1_boltztrap"
UNIFORM_DIR = "step3_uniform"          # 密网格 vasprun 来源
AMSET_DIR   = "step8_amset"            # 读 2d_correction.json 拿 c/t（若有）

TEMPERATURES = [100, 200, 300, 400, 500, 600, 700, 800, 900]  # K
# patch_mu_window：CRTA 类文献普遍把 μ 扫到带边上下 ±3 eV，ZT 峰
#   常落在 μ ≈ −1.5 eV。±1 eV 盖不到，没法与文献对点比。
MU_WINDOW_EV = 3.0        # 化学势扫描范围：带边上下各 ±MU_WINDOW_EV（eV）
MU_NPTS      = 1200       # 化学势采样点数（随窗口同比放大保分辨率）
DOS_NPTS     = 4000       # BoltzTraP2 DOS 积分点数
KMESH_MULT   = 5          # 插值目标 k 点数 = DFT k 点数 × 该倍数
# patch_bt2_scissor：BoltzTraP2 原本直接拿 PBE 能带积分，而 amset 用 HSE
#   带隙——两边不是同一套能带，S/Lorenz 的"交叉验证"无效。
#   "auto" = 复用 ke-dft-cpu 的 band_summary.json（与 amset 同源）；
#   数值 = 手填目标带隙(eV)；None = 关掉剪刀（只在确属金属时用）。
SCISSOR_GAP_EV = "auto"
# ---- patch_merge81：文献口径（CRTA × DPT-τ）。本步单独即为文章的完整方法 ----
PAPER_SCAN = True         # 关掉就只出 per-τ 的 boltztrap_crta.json
PAPER_T_K  = 600.0        # 文献常报 600 K；须在上面 TEMPERATURES 里
DPT_DIR    = "step8.2_dpt"
# patch_kappaL：晶格热导率来源。
#   "auto" = 自动读 kl-dft-cpu 技能的 step6_kappa/kappa_summary.json（推荐）。
#            只取其中的**原始** kappa_xx_yy_zz（元胞口径），再套本脚本的
#            NORM_2D 因子 —— 全流程只有一个 t，σ/κ_e/κ_L 的口径不可能错开。
#   None   = 不自动读，只用下面手填的值。
KAPPA_L_SOURCE = "auto"
KL_KAPPA_DIRS  = ("step6_kappa", "step6_kappa_shengbte")
KL_ROOT        = None     # kl-dft-cpu 与 ke-dft-cpu 不在同一材料目录时，填 kl-dft-cpu 的材料目录
# 手填值（W/mK，元胞口径，即 phono3py 原样）。非 None 时**优先于** auto。
KAPPA_L_XX_W_MK = None
KAPPA_L_YY_W_MK = None
# 2D 归一化口径。σ、κ_e 默认按含真空的元胞体积归一（σ ∝ 1/L_vac，人为量）；
#   "thickness" 按有效厚度 t 重标度成层材料等效三维值（文献主流口径，
#   TMD 族 t 取体相层间距）。★ ZT 对口径不变，变的是 σ/PF/浓度的绝对值。
#   κ_L 不用手动折算——脚本对 σ、κ_e、κ_L 同乘同一因子。
NORM_2D     = "thickness" # "cell" | "thickness"；thickness=按有效厚度 t 重标度（文献主流口径）
THICKNESS_A = None        # None = 读 step8_amset/2d_correction.json
SOMMERFELD_L = 2.44e-8    # WΩ/K²
# patch_align_n：把 μ 扫描的结果落到 amset 用的那套载流子浓度上，便于逐行并排比。
#   "amset" = 从 step8_amset/transport.json 读 doping 数组（推荐）
#   None    = 关掉；也可直接给一个列表（带符号，负=n型电子、正=p型空穴）
DOPING_ALIGN = "amset"
DOPING_LIST  = [-1e20, -1e19, -1e18, 1e18, 1e19, 1e20]   # 读不到 amset 时兜底
# =================================================================

_STEP1_CANDS = ("step1_opt", "step1_std_opt",
                "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")


def _read_dim(cwd):
    """从 step1 的 workflow_method.txt 读 DIM=（2d/3d/0d）；读不到返回 None。"""
    for name in _STEP1_CANDS:
        mf = Path(cwd) / name / "workflow_method.txt"
        if not mf.is_file():
            continue
        for ln in mf.read_text(errors="ignore").splitlines():
            if ln.strip().upper().startswith("DIM="):
                return ln.split("=", 1)[1].strip().lower()
    return None


def _guard_not_0d(cwd):
    if _read_dim(cwd) == "0d":
        sys.exit("[ERROR] step8.1_boltztrap 不支持 0D 体系（孤立分子无能带色散，"
                 "BoltzTraP2 输运无意义）。支持 2D/3D。")


def _need(mod, pip_name=None):
    try:
        return __import__(mod)
    except ImportError:
        sys.exit("[ERROR] 缺少 %s。请在 tf 运行环境里安装：pip install %s"
                 % (mod, pip_name or mod))


def _find_vasprun(d):
    for n in ("vasprun.xml", "vasprun.xml.gz"):
        p = Path(d) / n
        if p.is_file():
            return p
    return None


def _read_ct_factor(cwd):
    """2D：从 step8_amset/2d_correction.json 读 c/t 与 c、t（没有则 None）。"""
    p = Path(cwd) / AMSET_DIR / "2d_correction.json"
    try:
        rec = json.loads(p.read_text())
        return (float(rec["elastic_rescale_factor_c_over_t"]),
                float(rec.get("cell_c_A") or 0) or None,
                float((rec.get("layer_thickness") or {}).get("thickness_used_A") or 0) or None)
    except (OSError, KeyError, ValueError, TypeError):
        return None, None, None


# === patch_bt2_scissor：剪刀算符（把导带整体上移到目标带隙）===
_BANDGAP_PLOT_CANDS = ["step2_bandgap/step2.2_pbe_plot",
                       "step2_bandgap/step2.3_hse_plot",
                       "step4_band_plot"]


def _read_target_gap(cwd):
    """剪刀目标带隙。SCISSOR_GAP_EV 是数值就用它；"auto" 则读 ke-dft-cpu 的
    band_summary.json —— 与 step8_amset 的 bandgap 同源，保证两边同一套能带。"""
    if isinstance(SCISSOR_GAP_EV, (int, float)):
        return float(SCISSOR_GAP_EV)
    if str(SCISSOR_GAP_EV).lower() != "auto":
        return None
    for c in _BANDGAP_PLOT_CANDS:
        p = Path(cwd) / c / "band_summary.json"
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        for k in ("gap_eV", "band_gap", "bandgap", "gap", "Egap"):
            if k in d and float(d[k]) > 0:
                print("[..] 剪刀目标带隙 %.4f eV（来源 %s/band_summary.json）"
                      % (float(d[k]), c))
                return float(d[k])
    p = Path(cwd) / "project_setting" / "setting.yaml"
    if p.is_file():
        import re as _re
        for ln in p.read_text(errors="ignore").splitlines():
            m = _re.match(r"\s*bandgap\s*:\s*([\d.]+)", ln)
            if m:
                print("[..] 剪刀目标带隙 %.4f eV（来源 project_setting/setting.yaml）"
                      % float(m.group(1)))
                return float(m.group(1))
    return None


def _apply_scissor(data, target_gap_eV, Ha):
    """把 DFTData 的导带整体上移，使带隙 = target_gap_eV。必须在 fite.fitde3D
    之前调用（fitde3D 拟合的就是 data.ebands）。返回记录 dict。

    价/导带用"整条带在费米能同一侧"判定，与自旋极化无关；有带跨越费米能
    （金属/半金属）就跳过，绝不猜。"""
    info = {"applied": False, "target_gap_eV": target_gap_eV}
    if not target_gap_eV or float(target_gap_eV) <= 0:
        info["reason"] = "没拿到目标带隙——BoltzTraP2 用裸 DFT(PBE) 能带"
        print("[WARN] 未做剪刀修正：%s。与 amset 的 S/Lorenz 对比不成立。"
              % info["reason"])
        return info
    import numpy as np
    eb = np.asarray(data.ebands, dtype=float)      # (nband, nkpt) Hartree
    if eb.ndim != 2:
        info["reason"] = "ebands 形状异常 %s" % (eb.shape,)
        print("[WARN] 未做剪刀修正：%s" % info["reason"])
        return info
    ef = float(getattr(data, "fermi", 0.0))
    vmax, cmin = eb.max(axis=1), eb.min(axis=1)
    val = np.where(vmax <= ef)[0]
    con = np.where(cmin > ef)[0]
    if len(val) == 0 or len(con) == 0 or len(val) + len(con) != eb.shape[0]:
        info["reason"] = ("有能带跨越费米能（价带 %d / 导带 %d / 总 %d）——"
                          "金属或半金属，剪刀无意义"
                          % (len(val), len(con), eb.shape[0]))
        print("[WARN] 跳过剪刀修正：%s" % info["reason"])
        return info
    vbm, cbm = float(vmax[val].max()), float(cmin[con].min())
    gap_dft = (cbm - vbm) * Ha
    shift_eV = float(target_gap_eV) - gap_dft
    eb[con, :] += shift_eV / Ha
    data.ebands = eb
    data.fermi = vbm + (cbm - vbm + shift_eV / Ha) / 2.0   # 重新对中到带隙中央
    info.update({"applied": True,
                 "dft_gap_eV": round(gap_dft, 4),
                 "shift_eV": round(shift_eV, 4),
                 "n_valence_bands": int(len(val)),
                 "n_conduction_bands": int(len(con))})
    print("[OK] 剪刀修正：DFT 带隙 %.4f eV -> %.4f eV（导带上移 %.4f eV，"
          "费米能重新对中）" % (gap_dft, float(target_gap_eV), shift_eV))
    return info


def run_boltztrap_crta(uniform_dir, temperatures, mu_window_ev, mu_npts,
                       dos_npts, kmesh_mult, nelect=None, is_2d=False,
                       scissor_gap_eV=None):
    """核心：跑 BoltzTraP2 CRTA。uniform_dir 是**含 vasprun.xml 的目录**
    （DFTData 自己进目录找文件）。is_2d 时张量取面内 (xx+yy)/2（不含真空 zz）。
    返回结果 dict。可单测。"""
    import numpy as np
    from BoltzTraP2 import dft, sphere, fite, bandlib
    from BoltzTraP2 import units as U

    Ha = 27.211386245988          # eV/Hartree
    bohr_cm = 0.529177210903e-8   # cm/Bohr

    data = dft.DFTData(str(uniform_dir))          # 传目录，DFTData 自动找 vasprun.xml
    if nelect is None:
        nelect = getattr(data, "nelect", None)
    # patch_bt2_scissor：必须在 fitde3D 之前改 ebands
    scissor_info = _apply_scissor(data, scissor_gap_eV, Ha)
    equivalences = sphere.get_equivalences(
        data.atoms, data.magmom, len(data.kpoints) * kmesh_mult)
    coeffs = fite.fitde3D(data, equivalences)
    eband, vvband, cband = fite.getBTPbands(
        equivalences, coeffs, data.get_lattvec())
    epsilon, dos, vvdos, cdos = bandlib.BTPDOS(eband, vvband, npts=dos_npts)

    # 化学势窗口：以 DFT 费米能为中心，带边上下各 ±window
    efermi = float(getattr(data, "fermi", 0.0))   # Hartree
    win = mu_window_ev / Ha
    mur = np.linspace(efermi - win, efermi + win, int(mu_npts))
    Tr = np.array([float(t) for t in temperatures])

    N, L0, L1, L2, Lm11 = bandlib.fermiintegrals(
        epsilon, dos, vvdos, mur=mur, Tr=Tr, dosweight=data.dosweight)
    vuc = data.get_volume()                       # Bohr^3
    sigma, seebeck, kappa, _ = bandlib.calc_Onsager_coefficients(
        L0, L1, L2, mur, Tr, vuc)                 # σ/τ [S/m/s], S [V/K], κ_e/τ [W/m/K/s]

    vuc_cm3 = vuc * bohr_cm**3
    # 张量各向同性化：3D 取对角平均 (xx+yy+zz)/3；2D 取面内 (xx+yy)/2（不含真空 zz）
    def diag_avg(t):  # (nT,nmu,3,3) -> (nT,nmu)
        if is_2d:
            return (t[..., 0, 0] + t[..., 1, 1]) / 2.0
        return (t[..., 0, 0] + t[..., 1, 1] + t[..., 2, 2]) / 3.0

    sig = diag_avg(sigma)          # σ/τ
    sbk = diag_avg(seebeck)        # S
    kap = diag_avg(kappa)          # κ_e/τ
    # patch_bt2_aniso：面内平均会抹掉方向信息，另存 xx/yy 原始对角分量。
    #   文献（CRTA 类 2D 热电工作）的 σ/PF/ZT 图都是 x、y 分开画的。
    sig_xx, sig_yy = sigma[..., 0, 0], sigma[..., 1, 1]
    sbk_xx, sbk_yy = seebeck[..., 0, 0], seebeck[..., 1, 1]
    kap_xx, kap_yy = kappa[..., 0, 0], kappa[..., 1, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        lorenz = np.where(sig > 0, kap / (sig * Tr[:, None]), np.nan)
        pf = sbk**2 * sig          # S^2 σ/τ

    # 载流子浓度 n(μ,T)：BoltzTraP2 的 N 是负电子数，净载流子 = N + nelect
    # （见 BoltzTraP2 interface.py: y = N + data.nelect）。
    # 约定：负=n型(电子)、正=p型(空穴)，与 amset 一致。cm^-3。
    carr = None
    if nelect is not None:
        carr = (N + float(nelect)) / vuc_cm3

    mu_ev = (mur - efermi) * Ha    # 相对费米能，eV
    return {
        "scissor": scissor_info,          # patch_bt2_scissor
        "temperatures_K": [float(t) for t in Tr],
        "mu_rel_efermi_eV": [round(float(x), 5) for x in mu_ev],
        "cell_volume_cm3": vuc_cm3,
        "nelect": (float(nelect) if nelect is not None else None),
        # 张量对角平均，形状 [nT][nmu]
        "sigma_over_tau_S_per_m_s": sig.tolist(),
        "seebeck_V_per_K": sbk.tolist(),
        "kappa_e_over_tau_W_per_m_K_s": kap.tolist(),
        # patch_bt2_aniso：分方向（2D 面内 xx/yy），形状同上 [nT][nmu]
        "sigma_over_tau_xx": sig_xx.tolist(),
        "sigma_over_tau_yy": sig_yy.tolist(),
        "seebeck_xx_V_per_K": sbk_xx.tolist(),
        "seebeck_yy_V_per_K": sbk_yy.tolist(),
        "kappa_e_over_tau_xx": kap_xx.tolist(),
        "kappa_e_over_tau_yy": kap_yy.tolist(),
        "power_factor_over_tau": pf.tolist(),
        "lorenz_WOhm_per_K2": lorenz.tolist(),
        "carrier_conc_cm-3": (carr.tolist() if carr is not None else None),
        "note": ("σ、κ_e 为 per-τ（乘上常数 τ 得绝对值）；S 与 τ 无关；"
                 "Lorenz L=κ_e/(σT) 里 τ 抵消，可直接与 amset/文献比。"
                 "载流子浓度符号：- 为电子(n 型)、+ 为空穴(p 型)，与 amset 一致。"),
    }


# ======================= patch_merge81：文献口径复现 =======================
def _dpt_module():
    """按文件路径加载同级技能目录的 gen_step12_dpt.py（拿各向异性 τ）。"""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "step8.2_dpt" / "gen_step12_dpt.py"
    if not p.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_ke_dpt", str(p))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception as e:                                    # noqa: BLE001
        print("[WARN] 加载 DPT 模块失败（%s）——改从 %s/dpt_result.json 读"
              % (type(e).__name__, DPT_DIR))
        return None


def _tau_from_json(cwd):
    """回退通道：读 step8.2_dpt/dpt_result.json。"""
    import json
    p = Path(cwd) / DPT_DIR / "dpt_result.json"
    if not p.is_file():
        return {}
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    M0, E_C = 9.1093837015e-31, 1.602176634e-19
    out = {}
    for r in j.get("results", []):
        c, bd = r.get("carrier"), (r.get("by_direction") or {})
        if bd.get("status") == "ok":
            out[c] = {d: bd[d]["tau_s"] for d in ("x", "y")}
            continue
        mu = r.get("mobility_cm2_Vs")
        me = (r.get("inputs") or {}).get("m_eff_m0")
        if isinstance(mu, (int, float)) and isinstance(me, (int, float)) \
                and mu > 0 and me > 0:
            t = (mu * 1e-4) * (me * M0) / E_C
            out[c] = {"x": t, "y": t}
            print("[WARN] %s 无分方向 τ，回落各向同性" % c)
    return out


def _tau_aniso(cwd, is_2d, T):
    """{'electron': {'x':τ,'y':τ}, 'hole': {...}}。优先直接调 DPT 模块。"""
    m = _dpt_module()
    if m is not None:
        try:
            out = {}
            for c in ("electron", "hole"):
                bd = m._aniso_block(Path(cwd), is_2d, c, T)
                if bd.get("status") == "ok":
                    out[c] = {d: bd[d]["tau_s"] for d in ("x", "y")}
                else:
                    print("[WARN] DPT %s 分方向不可用：%s" % (c, bd.get("status")))
            if out:
                print("[OK] DPT 分方向 τ：" + "；".join(
                    "%s x=%.1f fs y=%.1f fs" % (c, v["x"] * 1e15, v["y"] * 1e15)
                    for c, v in out.items()))
                return out
        except Exception as e:                                # noqa: BLE001
            print("[WARN] 直接调用 DPT 失败（%s）——改读 json" % type(e).__name__)
    return _tau_from_json(cwd)


def _norm_2d(cwd):
    """返回 (factor, info)。factor 同时乘在 σ、κ_e、κ_L 上。"""
    import json
    rec = {}
    p = Path(cwd) / AMSET_DIR / "2d_correction.json"
    if p.is_file():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
    c = rec.get("cell_c_A")
    t = THICKNESS_A if THICKNESS_A else rec.get("thickness_used_A")
    f = rec.get("elastic_rescale_factor_c_over_t")
    if THICKNESS_A and c:
        f = c / float(THICKNESS_A)
    info = {"norm": NORM_2D, "cell_c_A": c, "thickness_A": t, "c_over_t": f,
            "factor_applied": 1.0}
    if str(NORM_2D).lower() != "thickness":
        return 1.0, info
    if not f:
        print("[WARN] NORM_2D='thickness' 但读不到 c/t（缺 %s/2d_correction.json），"
              "回退 cell 口径" % AMSET_DIR)
        info["norm"] = "cell(回退)"
        return 1.0, info
    info["factor_applied"] = float(f)
    print("[..] 2D 口径 thickness：σ/κ_e/κ_L 同乘 c/t = %.4f（c=%.3f Å, t=%.3f Å）"
          % (f, c or float("nan"), t or float("nan")))
    return float(f), info


def _interp_T(temps, vals, T):
    """按温度线性插值；超出网格范围返回 None（不外推）。"""
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


def _resolve_kappa_L(cwd, T, ninfo):
    """返回 (kxx, kyy, lines)。手填优先；否则读 kl-dft-cpu 的 kappa_summary.json。

    ★ 只取原始 kappa_xx_yy_zz（元胞口径），再乘 ninfo['factor_applied']，
      保证与 σ、κ_e 用同一个 t。绝不使用 kl-dft-cpu 自己的 kappa_2d_normalized ——
      那用的是 kl-dft-cpu 的 vdW 表，未必等于 ke-dft-cpu 的 t。"""
    import json
    fac = ninfo.get("factor_applied", 1.0)
    if KAPPA_L_XX_W_MK is not None or KAPPA_L_YY_W_MK is not None:
        kx = KAPPA_L_XX_W_MK * fac if KAPPA_L_XX_W_MK is not None else None
        ky = KAPPA_L_YY_W_MK * fac if KAPPA_L_YY_W_MK is not None else None
        return kx, ky, ["# kappa_L 来源：手填（元胞口径 xx=%s yy=%s，已套 %s 因子 %.4f）"
                        % (KAPPA_L_XX_W_MK, KAPPA_L_YY_W_MK, ninfo["norm"], fac)]
    if not KAPPA_L_SOURCE or str(KAPPA_L_SOURCE).lower() != "auto":
        return None, None, ["# kappa_L 未提供（KAPPA_L_SOURCE 非 auto 且未手填）——不出 ZT"]

    root = Path(KL_ROOT) if KL_ROOT else Path(cwd)
    src = None
    for d in KL_KAPPA_DIRS:
        p = root / d / "kappa_summary.json"
        if p.is_file():
            src = p
            break
    if src is None:
        return None, None, ["# kappa_L 未找到：%s 下没有 %s/kappa_summary.json"
                            % (root, "|".join(KL_KAPPA_DIRS)),
                            "#   kl-dft-cpu 跑完了吗？或用 KL_ROOT 指定 kl-dft-cpu 的材料目录"]
    try:
        j = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, None, ["# kappa_L 读取失败：%s（%s）" % (src, type(e).__name__)]

    raw = j.get("kappa_xx_yy_zz")
    temps = j.get("temperatures")
    if not raw or not temps:
        return None, None, ["# kappa_L 读取失败：%s 里没有 kappa_xx_yy_zz/temperatures"
                            % src.name]
    kxx = _interp_T(temps, [r[0] for r in raw], T)
    kyy = _interp_T(temps, [r[1] for r in raw], T)
    lines = ["# kappa_L 来源：%s（原始元胞口径 → 套本步 %s 因子 %.4f）"
             % (os.path.relpath(str(src), str(root)), ninfo["norm"], fac)]
    if kxx is None or kyy is None:
        lines.append("#   T=%.0f K 超出 kl-dft-cpu 的温度网格 [%.0f, %.0f] —— 拒绝外推，不出 ZT"
                     % (T, min(temps), max(temps)))
        return None, None, lines
    lines.append("#   kappa_L(%.0f K) 元胞口径 xx=%.4f yy=%.4f W/mK" % (T, kxx, kyy))

    # ---- 自动闸门：元胞与厚度口径一致性 ----
    Lz, cA = j.get("Lz_ang"), ninfo.get("cell_c_A")
    if Lz and cA:
        dc = abs(float(Lz) - float(cA)) / float(cA) * 100
        if dc > 1.0:
            raise SystemExit(
                "[ERROR] kl-dft-cpu 与 ke-dft-cpu 的元胞不一致：kl-dft-cpu Lz=%.4f Å，ke-dft-cpu c=%.4f Å（差 %.2f%%）。\n"
                "        两边 step1 都是 ISIF=3 + IOPTCELL 冻结 c，同一份输入 POSCAR "
                "本不该出现这种差异。\n"
                "        请查两个技能的 step.conf 里 CELL_POLICY / "
                "VACUUM_AXIS_POLICY 是否被单边覆盖，或输入结构是否真的同一份。"
                % (Lz, cA, dc))
        lines.append("#   [闸门] 元胞一致：kl-dft-cpu Lz=%.4f Å vs ke-dft-cpu c=%.4f Å（差 %.3f%%）"
                     % (Lz, cA, dc))
    else:
        lines.append("#   [闸门] 跳过元胞核对（kl-dft-cpu 或 ke-dft-cpu 缺 Lz/c 元数据）")

    dkl, dke = j.get("thickness_d_ang"), ninfo.get("thickness_A")
    if dkl and dke and abs(float(dkl) - float(dke)) > 0.01:
        lines.append("#   [注意] 厚度取值不同：kl-dft-cpu d=%.3f Å（%s）vs ke-dft-cpu t=%.3f Å。"
                     % (dkl, j.get("thickness_convention", "?"), dke))
        lines.append("#          两边 vdW 半径表有 15 个元素不一致（多为过渡金属）。")
        lines.append("#          ★ ZT 不受影响：本步走的是 kl-dft-cpu 的**原始** κ，"
                     "再套 ke-dft-cpu 自己的因子，全流程只有一个 t。")
        lines.append("#          但 kl-dft-cpu 自己写的 kappa_2d_normalized 与本步口径不同，别混用。")
    return kxx * fac, kyy * fac, lines


def _doping_targets(cwd):
    """amset 用的掺杂浓度列表（带符号）。读不到就用 DOPING_LIST。"""
    import json
    if not DOPING_ALIGN:
        return None, "关闭"
    if isinstance(DOPING_ALIGN, (list, tuple)):
        return [float(x) for x in DOPING_ALIGN], "手填列表"
    if str(DOPING_ALIGN).lower() != "amset":
        return None, "DOPING_ALIGN 取值无法识别"
    p = Path(cwd) / AMSET_DIR / "transport.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8")).get("doping")
            if d:
                return [float(x) for x in d], "%s/transport.json" % AMSET_DIR
        except (OSError, ValueError):
            pass
    return [float(x) for x in DOPING_LIST], "DOPING_LIST 兜底（amset 结果没读到）"


def _mu_at_doping(mu, carr, n_target):
    """由目标净载流子浓度反解 μ。只在**同号分支内**对 log10|n| 插值，不外推：
    净浓度对 μ 单调，但跨带隙时符号翻转、量级横跨十几个数量级。"""
    import math
    idx = [i for i, c in enumerate(carr)
           if c == c and c != 0 and (c > 0) == (n_target > 0)]
    if len(idx) < 2:
        return None
    pts = sorted((math.log10(abs(carr[i])), mu[i]) for i in idx)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xt = math.log10(abs(n_target))
    if xt < xs[0] - 1e-12 or xt > xs[-1] + 1e-12:
        return None
    for i in range(len(xs) - 1):
        if xs[i] - 1e-12 <= xt <= xs[i + 1] + 1e-12:
            if abs(xs[i + 1] - xs[i]) < 1e-15:
                return ys[i]
            w = (xt - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] * (1 - w) + ys[i + 1] * w
    return ys[-1]


def _at_mu(mu, vals, mu_t):
    """线性插值到 μ_t（μ 网格已排序且很密）。"""
    if mu_t is None or not vals:
        return None
    if mu_t <= mu[0]:
        return vals[0]
    if mu_t >= mu[-1]:
        return vals[-1]
    for i in range(len(mu) - 1):
        if mu[i] <= mu_t <= mu[i + 1]:
            a, b = vals[i], vals[i + 1]
            if a is None or b is None or a != a or b != b:
                return None
            if abs(mu[i + 1] - mu[i]) < 1e-15:
                return a
            w = (mu_t - mu[i]) / (mu[i + 1] - mu[i])
            return a * (1 - w) + b * w
    return vals[-1]


def write_doping_aligned(out, cwd, rows, T, ninfo):
    """把 μ 扫描落到 amset 的掺杂浓度上，逐行可与 comparison_300K.csv 并排比。"""
    import csv
    targets, src = _doping_targets(cwd)
    if not targets:
        return ["# 掺杂对齐关闭（DOPING_ALIGN=%r）" % DOPING_ALIGN]
    mu = [r["mu_rel_efermi_eV"] for r in rows]
    carr = [r["carrier_conc_cm-3"] for r in rows]
    cA = ninfo.get("cell_c_A") or 0.0
    keys = [k for k in rows[0].keys()
            if k not in ("mu_rel_efermi_eV", "carrier_conc_cm-3", "branch",
                         "n_2D_cm-2")]
    outrows, missed = [], []
    for n in targets:
        mu_t = _mu_at_doping(mu, carr, n)
        if mu_t is None:
            missed.append(n)
            continue
        r = {"carrier_conc_cm-3": n, "n_2D_cm-2": (n * cA * 1e-8) if cA else None,
             "type": "n(电子)" if n < 0 else "p(空穴)", "mu_rel_efermi_eV": mu_t}
        for k in keys:
            r[k] = _at_mu(mu, [x.get(k) for x in rows], mu_t)
        outrows.append(r)
    if not outrows:
        return ["# 掺杂对齐失败：所有目标浓度都在 μ 窗口之外，把 MU_WINDOW_EV 开大"]
    cols = (["carrier_conc_cm-3", "n_2D_cm-2", "type", "mu_rel_efermi_eV"]
            + [k for k in keys if any(r.get(k) is not None for r in outrows)])
    p = out / ("paper_at_doping_%dK.csv" % round(T))
    with open(p, "w", newline="") as fh:
        fh.write("# CRTA x DPT-tau 落到 amset 的掺杂浓度上；T=%.0f K；来源 %s；"
                 "norm=%s, factor=%.4f\n"
                 % (T, src, ninfo["norm"], ninfo.get("factor_applied", 1.0)))
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in outrows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    lines = ["", "# ---- 按载流子浓度对齐 amset：%s ----" % p.name,
             "# 掺杂列表来源：%s（%d 个点，命中 %d 个）"
             % (src, len(targets), len(outrows))]
    if missed:
        lines.append("# 未命中（超出 μ 窗口内该分支的浓度范围，未外推）：%s"
                     % "、".join("%.0e" % x for x in missed))
    lines.append("# [口径] BoltzTraP2 的 n 与 amset 的 doping 都按含真空的元胞体积"
                 "定义，符号约定同为 负=n型/正=p型")
    if cA:
        lines.append("#        元胞 c=%.4f Å；n_2D = n_3D × c 跨口径不歧义" % cA)
    return lines


def write_paper_scan(out, cwd, res, is_2d):
    """CRTA × DPT-τ 的化学势扫描，分方向。返回摘要行列表。"""
    import csv
    if not res.get("sigma_over_tau_xx"):
        return ["# 文献口径扫描跳过：缺分方向数组"]
    Ts = res["temperatures_K"]
    iT = min(range(len(Ts)), key=lambda i: abs(Ts[i] - PAPER_T_K))
    T = float(Ts[iT])
    if abs(T - PAPER_T_K) > 1.0:
        print("[WARN] TEMPERATURES 里没有 %.0f K，改用最近的 %.0f K" % (PAPER_T_K, T))
    carr = (res.get("carrier_conc_cm-3") or [None] * len(Ts))[iT]
    if carr is None:
        return ["# 文献口径扫描跳过：没有载流子浓度（nelect 没读到），无法分 n/p 支"]
    taus = _tau_aniso(cwd, is_2d, T)
    if not taus:
        return ["# 文献口径扫描跳过：拿不到 DPT 的 τ（step8.2 跑了吗？）"]

    fac, ninfo = _norm_2d(cwd)
    cA = ninfo.get("cell_c_A") or 0.0
    kl_xx, kl_yy, kl_lines = _resolve_kappa_L(cwd, T, ninfo)   # patch_kappaL
    for _l in kl_lines:
        print(_l)
    mu = res["mu_rel_efermi_eV"]
    sot = {"xx": res["sigma_over_tau_xx"][iT], "yy": res["sigma_over_tau_yy"][iT]}
    sbk = {"xx": res["seebeck_xx_V_per_K"][iT], "yy": res["seebeck_yy_V_per_K"][iT]}

    rows = []
    for i, m in enumerate(mu):
        n = carr[i]
        branch = "electron" if n < 0 else "hole"   # 负=n型、正=p型（全流程约定）
        tb = taus.get(branch) or {}
        r = {"mu_rel_efermi_eV": m, "carrier_conc_cm-3": n, "branch": branch,
             "n_2D_cm-2": (n * cA * 1e-8) if cA else None}
        for d, dd, kl in (("xx", "x", kl_xx), ("yy", "y", kl_yy)):   # patch_kappaL
            S = sbk[d][i] * 1e6                     # V/K -> µV/K，与 τ 无关
            r["S_%s_uV/K" % d] = S
            tau = tb.get(dd)
            r["tau_%s_fs" % d] = (tau * 1e15) if tau else None
            if not tau or S != S:
                continue
            sig = sot[d][i] * tau * fac
            ke = SOMMERFELD_L * sig * T
            pf = (S * 1e-6) ** 2 * sig
            r["sigma_%s_S/m" % d] = sig
            r["PF_%s_W/mK2" % d] = pf
            r["kappa_e_WF_%s_W/mK" % d] = ke
            r["sheet_sigma_%s_S" % d] = (sig / fac) * (cA * 1e-10) if cA else None
            if kl is not None and (kl + ke) > 0:      # kl-dft-cpu 已含口径因子
                r["ZT_%s" % d] = pf * T / (kl + ke)
        rows.append(r)

    cols = ["mu_rel_efermi_eV", "carrier_conc_cm-3", "n_2D_cm-2", "branch",
            "tau_xx_fs", "tau_yy_fs", "S_xx_uV/K", "S_yy_uV/K",
            "sigma_xx_S/m", "sigma_yy_S/m", "sheet_sigma_xx_S", "sheet_sigma_yy_S",
            "PF_xx_W/mK2", "PF_yy_W/mK2",
            "kappa_e_WF_xx_W/mK", "kappa_e_WF_yy_W/mK"]
    cols += [c for c in ("ZT_xx", "ZT_yy") if any(c in r for r in rows)]
    p = out / ("paper_mu_scan_%dK.csv" % round(T))
    with open(p, "w", newline="") as fh:
        fh.write("# CRTA(BoltzTraP2) x DPT-tau 文献口径；T=%.0f K；"
                 "kappa_e=L*sigma*T (WF)；norm=%s, c=%s A, t=%s A, factor=%.4f\n"
                 % (T, ninfo["norm"], ninfo.get("cell_c_A"),
                    ninfo.get("thickness_A"), ninfo["factor_applied"]))
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})

    lines = ["", "# ---- 文献口径（CRTA x DPT-tau）：%s ----" % p.name,
             "# T=%.0f K；sigma_a=(sigma_a/tau)*tau_a 分方向；"
             "kappa_e=L*sigma*T（WF, 文献 Eq.5）" % T,
             "# 2D 口径 %s（c=%s A, t=%s A, 因子=%.4f）—— ZT 对口径不变"
             % (ninfo["norm"], ninfo.get("cell_c_A"), ninfo.get("thickness_A"),
                ninfo["factor_applied"])]
    lines += kl_lines                                   # patch_kappaL
    for c, tb in taus.items():
        lines.append("# tau_%-8s x=%.1f fs  y=%.1f fs"
                     % (c, tb["x"] * 1e15, tb["y"] * 1e15))
    for key in ("ZT_xx", "ZT_yy", "PF_xx_W/mK2", "PF_yy_W/mK2"):
        best = max((r for r in rows if r.get(key) is not None),
                   key=lambda r: r[key], default=None)
        if best:
            lines.append("# %-14s 峰值 %.4g @ mu=%+.3f eV（n=%.3e cm^-3，%s 支）"
                         % (key, best[key], best["mu_rel_efermi_eV"],
                            best["carrier_conc_cm-3"], best["branch"]))
        elif key.startswith("ZT"):
            lines.append("# %-14s 未算（没拿到 kappa_L，见上）" % key)

    try:                                            # patch_align_n
        lines += write_doping_aligned(out, cwd, rows, T, ninfo)
    except Exception as e:                          # noqa: BLE001
        lines.append("# 掺杂对齐失败：%s" % type(e).__name__)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        panels = [("sigma_xx_S/m", "sigma_yy_S/m", "sigma (S/m)"),
                  ("S_xx_uV/K", "S_yy_uV/K", "S (uV/K)"),
                  ("PF_xx_W/mK2", "PF_yy_W/mK2", "PF (W/m/K^2)"),
                  ("ZT_xx", "ZT_yy", "ZT")]
        fig, axes = plt.subplots(2, 2, figsize=(11, 8))
        x = [r["mu_rel_efermi_eV"] for r in rows]
        for ax, (kx, ky, lab) in zip(axes.ravel(), panels):
            got = False
            for k, sty, nm in ((kx, "-", "x-axis"), (ky, "--", "y-axis")):
                y = [r.get(k) for r in rows]
                if any(v is not None for v in y):
                    ax.plot(x, [(v if v is not None else float("nan")) for v in y],
                            sty, label=nm)
                    got = True
            ax.set_xlabel("mu - E_F (eV)")
            ax.set_ylabel(lab)
            ax.axvline(0.0, color="0.7", lw=0.8)
            if got:
                ax.legend(fontsize=8)
            else:
                ax.text(0.5, 0.5, "fill KAPPA_L_XX/YY", ha="center",
                        transform=ax.transAxes, fontsize=9)
        fig.suptitle("CRTA x DPT (paper protocol), %.0f K, norm=%s"
                     % (T, ninfo["norm"]))
        fig.tight_layout()
        fig.savefig(out / ("paper_mu_scan_%dK.png" % round(T)), dpi=150)
        plt.close(fig)
    except Exception as e:                                    # noqa: BLE001
        lines.append("# 画图跳过：%s" % type(e).__name__)
    return lines


def main():
    cwd = Path.cwd()
    _guard_not_0d(cwd)
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)

    _need("BoltzTraP2", "BoltzTraP2")
    vr_dir = cwd / UNIFORM_DIR
    if _find_vasprun(vr_dir) is None:
        sys.exit("[ERROR] 找不到 %s/vasprun.xml（step3_uniform 没算完？）" % UNIFORM_DIR)

    # 取 nelect（BoltzTraP2 有就用；否则用 pymatgen 兜底）
    nelect = None
    try:
        pmg = __import__("pymatgen.io.vasp", fromlist=["Vasprun"])
        vr = pmg.Vasprun(str(_find_vasprun(vr_dir)), parse_dos=False,
                         parse_eigen=False, parse_potcar_file=False)
        nelect = float(vr.parameters.get("NELECT")) if vr.parameters.get("NELECT") else None
    except Exception:
        nelect = None

    dim = _read_dim(cwd)
    print("[..] BoltzTraP2 CRTA 插值 + 输运 ...")
    # 注意：BoltzTraP2 的 DFTData 要的是"目录"（它自己进去找 vasprun.xml），
    # 不是 vasprun.xml 文件本身。2D 时张量取面内 (xx+yy)/2。
    res = run_boltztrap_crta(vr_dir, TEMPERATURES, MU_WINDOW_EV,
                             MU_NPTS, DOS_NPTS, KMESH_MULT, nelect=nelect,
                             is_2d=(dim == "2d"),
                             scissor_gap_eV=_read_target_gap(cwd))

    res["dim"] = dim
    if dim == "2d":
        res["tensor_average"] = "in-plane (xx+yy)/2"
        ct, c_A, t_A = _read_ct_factor(cwd)
        res["twoD_c_over_t"] = ct
        res["twoD_note"] = ("2D：张量已取面内 (xx+yy)/2；σ、κ_e 仍按超胞体积口径"
                            "（与 amset 一致，可直接对比）。要面内薄片口径再乘 c/t=%s。" % ct)

    (out / "boltztrap_crta.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人读摘要：列 300 K 下最优功率因子附近的几个点
    _write_summary(out, res)
    if PAPER_SCAN:                                     # patch_merge81
        try:
            extra = write_paper_scan(out, cwd, res, dim == "2d")
            with open(out / "boltztrap_summary.txt", "a", encoding="utf-8") as fh:
                fh.write("\n".join(extra) + "\n")
            print("\n".join(extra))
        except Exception as e:                          # noqa: BLE001
            print("[WARN] 文献口径扫描失败（%s）——per-τ 结果不受影响"
                  % type(e).__name__)
    print("[DONE] %s：boltztrap_crta.json 已生成" % OUTDIR_NAME)


def _write_summary(out, res):
    import math
    lines = ["# BoltzTraP2 CRTA 摘要（详见 boltztrap_crta.json）",
             "# dim=%s  nelect=%s" % (res.get("dim"), res.get("nelect"))]
    Ts = res["temperatures_K"]
    if 300.0 in Ts:
        iT = Ts.index(300.0)
        pf = res["power_factor_over_tau"][iT]
        # 找 |PF| 最大的 μ 点
        imax = max(range(len(pf)), key=lambda i: (pf[i] if pf[i] == pf[i] else -1))
        L = res["lorenz_WOhm_per_K2"][iT][imax]
        lines.append("300 K 处 PF/τ 最大点：")
        lines.append("  μ-E_F = %+.3f eV" % res["mu_rel_efermi_eV"][imax])
        lines.append("  σ/τ   = %.3e S/m/s" % res["sigma_over_tau_S_per_m_s"][iT][imax])
        lines.append("  S     = %+.1f µV/K" % (res["seebeck_V_per_K"][iT][imax] * 1e6))
        lines.append("  κ_e/τ = %.3e W/m/K/s" % res["kappa_e_over_tau_W_per_m_K_s"][iT][imax])
        lines.append("  Lorenz= %.3e WΩ/K²（Sommerfeld=2.44e-8）"
                     % (L if L == L else float("nan")))
        if res.get("carrier_conc_cm-3"):
            lines.append("  n     = %+.3e cm^-3（负=n型电子 / 正=p型空穴）"
                         % res["carrier_conc_cm-3"][iT][imax])
    (out / "boltztrap_summary.txt").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")


if __name__ == "__main__":
    main()