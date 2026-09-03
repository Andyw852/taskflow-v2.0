#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step12_dpt.py —— 形变势理论 DPT（Bardeen-Shockley）迁移率（step8.2_dpt）。

run:gen 步骤：登录节点直接跑、不提交 SLURM，秒级。
用经典形变势理论算声学支限制的载流子迁移率——这是 2D 输运文献最主流的做法，
用来和 step8 的 amset(ADP) 做对标/交叉验证。

公式（各向同性）：
  2D:  μ = e ℏ³ C_2D / (k_B T m* m_d E1²)          （Bardeen-Shockley 2D）
  3D:  μ = 2√(2π) e ℏ⁴ C_3D / (3 (k_B T)^{3/2} m*^{5/2} E1²)
其中 C 为弹性模量、m* 有效质量、E1 形变势。

输入来源（都可在下方 MANUAL 手填覆盖；自动值仅尽力而为、务必核对）：
  · C   ← step6_elastic/OUTCAR（可靠自动；2D 再乘层厚得 C_2D[N/m]）
  · m*  ← step3_uniform 能带曲率（BoltzTraP2 尽力自动）
  · E1  ← step7b_deform_read/deformation.h5 的带边形变势（尽力自动）

【重要诚实说明】m* 与 E1 的自动提取精度有限。amset 的 ADP 已经用 deformation.h5 做了
更严格的形变势散射；本步是"经典闭式对标"。要发表级 DPT，建议手填 MANUAL 里的
m*/E1/C（多数 DPT 论文就是这么做的），或核对自动值后再用。

产出（done_marker）：本步目录下的 dpt_result.json（+ summary.txt）。缺输入时写出
带指引的部分结果，绝不崩溃、绝不编造。
"""
import json
import math
import os
import sys
from pathlib import Path

# =========================== 可改参数区 ===========================
# [SKILL_REV] 版本戳：写进 dpt_result.json，铺开时验证跑的是哪份 skill 副本。
# 每次改本脚本逻辑后更新（如 "2026-08-29-nstep-linear"）。
_SKILL_REV = "2026-08-31-rscan"
# [R-SCAN] 强制选点壳层 NSTEP（None=自动 2/3/4/5；填 2/3/4/5=只试该值，做 R-scan 用）。
# 用法：改 FORCE_NSTEP → tf -p <材料> -j S8.2_dpt retry + start，对比 dpt_result.json 的 m_d 与 m_provenance 里的 R。
FORCE_NSTEP = None
OUTDIR_NAME = "step8.2_dpt"
UNIFORM_DIR = "step3_uniform"
ELASTIC_DIR = "step6_elastic"
DEFORM_READ_DIR = "step7b_deform_read"
AMSET_DIR   = "step8_amset"          # 读 2d_correction.json 拿层厚 t、c/t

TEMPERATURE_K = 300.0                # DPT 迁移率报此温度（μ∝1/T，可换算）
CARRIER = "both"                     # electron / hole / both
# E1 自动提取口径开关（手填 MANUAL/MANUAL_ANISO 时此开关失效）：
#   "vac"   = 真空对齐 E1_vac（文献 Eq.9 dE_edge/dγ 口径，仅 2D 且 LOCPOT 可用）
#   "amset" = amset h5 平均芯势口径（⟨|D|⟩）
# [C5] 符号约定：两套口径写出的字段【都恒非负】——vac 侧生成端已做
#   vals.append(abs(raw - dvac))，amset 侧 h5 本身是 ⟨|D|⟩。消费端仍补 abs()
#   兜底，但不要指望 E1_vac_* 带符号（μ 只依赖 E1²，符号无物理用途）。
# [C2] E1_SOURCE 是【硬选择】不是偏好：要 "vac" 却拿不到 E1_vac_* 时直接报错，
#   绝不静默回退 amset 口径（两者数值能差 3 倍，μ 差一个量级）。
E1_SOURCE = "vac"

# —— 手动覆盖（填了就用手填值，最可靠；None=尝试自动）——
MANUAL = {
    "m_eff_electron": None,   # m*/m0（各向同性；也可只填电子或空穴）
    "m_eff_hole":     None,
    "E1_electron_eV": None,   # 形变势 eV
    "E1_hole_eV":     None,
    "C_2D_N_per_m":   None,   # 2D 弹性模量 N/m（填了就不从 step6 推）
    "C_3D_GPa":       None,   # 3D 弹性模量 GPa
    "thickness_A":    None,   # 2D 层厚 Å（None=读 2d_correction.json）
}
# =================================================================

# 物理常数（SI）
E_C   = 1.602176634e-19
HBAR  = 1.054571817e-34
KB    = 1.380649e-23
M0    = 9.1093837015e-31

_STEP1_CANDS = ("step1_opt", "step1_std_opt",
                "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")


def _read_dim(cwd):
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
        sys.exit("[ERROR] step8.2_dpt 不支持 0D 体系（无能带色散，形变势无意义）。")


# ---------- [PATCH-KE-2026] 自旋道感知的带边定位 ----------
def _spin_arrays(v):
    """按 (up, down) 固定顺序返回各自旋道本征值 [(ispin, arr(nk,nb,2)), ...]。
    ISPIN=1 时只有一道，行为与打补丁前完全一致。"""
    import numpy as np
    eigmap = v.eigenvalues
    try:
        from pymatgen.electronic_structure.core import Spin
        order = [s for s in (Spin.up, Spin.down) if s in eigmap]
    except Exception:
        order = []
    if not order:
        order = list(eigmap.keys())
    return [(i, np.asarray(eigmap[s])) for i, s in enumerate(order)]


def _find_band_edge(v, carrier, occ_tol=0.5):
    """跨【所有】自旋道找全局 CBM/VBM。

    打补丁前用的是 list(v.eigenvalues.values())[0]，ISPIN=2 时永远只看
    spin-up，spin-down 的带边被丢掉 —— 对 FM 半导体/半金属会直接取错带。
    返回 (ispin, k0, b0, ene(nk,nb))；全占据/全空(金属)时返回 None。"""
    import numpy as np
    best = None
    for isp, eig in _spin_arrays(v):
        ene, occ = eig[:, :, 0], eig[:, :, 1]
        mask = (occ < occ_tol) if carrier == "electron" else (occ > occ_tol)
        if not mask.any():
            continue
        if carrier == "electron":
            cand = np.where(mask, ene, np.inf)
            k0, b0 = np.unravel_index(np.argmin(cand), cand.shape)
            val, better = cand[k0, b0], (best is None or cand[k0, b0] < best[0])
        else:
            cand = np.where(mask, ene, -np.inf)
            k0, b0 = np.unravel_index(np.argmax(cand), cand.shape)
            val, better = cand[k0, b0], (best is None or cand[k0, b0] > best[0])
        if better:
            best = (val, isp, int(k0), int(b0), ene)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def _sorted_dp_keys(f):
    """deformation.h5 里的 deformation_potentials 数据集，up 在前 down 在后。"""
    keys = [k for k in f.keys() if "deformation_potentials" in k]
    return sorted(keys, key=lambda s: ("down" in s.lower(), s))


# ---------- C：从 step6_elastic/OUTCAR 读面内弹性 ----------
def read_elastic_inplane_GPa(cwd):
    """返回 (C11, C22) GPa 面内对角；读不到返回 (None, None)。"""
    oc = Path(cwd) / ELASTIC_DIR / "OUTCAR"
    if not oc.is_file():
        return None, None
    import re
    txt = oc.read_text(errors="ignore")
    i = txt.rfind("TOTAL ELASTIC MODULI")
    if i < 0:
        return None, None
    rows = []
    for ln in txt[i:].splitlines()[1:]:
        nums = re.findall(r"-?\d+\.\d+", ln)
        if len(nums) >= 6:
            rows.append([float(x) for x in nums[:6]])
        if len(rows) == 6:
            break
    if len(rows) < 6:
        return None, None
    return rows[0][0] / 10.0, rows[1][1] / 10.0   # kBar->GPa, C11 C22


def _thickness_A(cwd):
    if MANUAL["thickness_A"]:
        return float(MANUAL["thickness_A"])
    p = Path(cwd) / AMSET_DIR / "2d_correction.json"
    try:
        rec = json.loads(p.read_text())
        return float((rec.get("layer_thickness") or {}).get("thickness_used_A"))
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _cell_height_A(cwd):
    """2D 元胞垂直层面的高度 h = V/A（体积÷面内面积），对非正交/c 倾斜也正确。
    从 POSCAR/CONTCAR 的三个格矢直接算；读不到再退回 2d_correction.json 的 cell_c_A。"""
    for name in ("step3_uniform", ELASTIC_DIR, "step8_amset"):
        for fn in ("POSCAR", "CONTCAR"):
            f = Path(cwd) / name / fn
            if not f.is_file():
                continue
            try:
                import numpy as np
                ln = f.read_text().splitlines()
                scale = float(ln[1].split()[0])
                a = np.array([float(x) for x in ln[2].split()[:3]]) * scale
                b = np.array([float(x) for x in ln[3].split()[:3]]) * scale
                c = np.array([float(x) for x in ln[4].split()[:3]]) * scale
                V = abs(np.dot(a, np.cross(b, c)))     # 体积
                A = np.linalg.norm(np.cross(a, b))     # 面内面积 |a×b|
                if A > 0 and V > 0:
                    return float(V / A)                # 垂直高度 h
            except (IndexError, ValueError):
                pass
    # 退回：json 里的 cell_c_A（注意它是 |c|，仅当 c 垂直层面时才等于 h）
    p = Path(cwd) / AMSET_DIR / "2d_correction.json"
    try:
        return float(json.loads(p.read_text())["cell_c_A"])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def get_C(cwd, is_2d):
    """返回 (C_value, C_unit, provenance)。2D 给 C_2D[N/m]，3D 给 C_3D[Pa]。
    2D 刚度 C_2D[N/m] = C_3D[Pa] × 元胞垂直高度 h=V/A —— VASP 的 C_3D 按整胞体积
    A·h 归一化，乘 h 得单位面积的 2D 模量（与厚度歧义无关，2D-DPT 标准定义，
    如 Qiao 等黑磷工作）。用 V/A 而非 |c|，对非正交/c 倾斜的胞也正确。"""
    if is_2d:
        if MANUAL["C_2D_N_per_m"]:
            return float(MANUAL["C_2D_N_per_m"]), "N/m", "manual"
        c11, c22 = read_elastic_inplane_GPa(cwd)
        h = _cell_height_A(cwd)
        if c11 is None or c22 is None or not h:
            return None, "N/m", "缺 step6 弹性或元胞高度 h=V/A"
        C_inplane_Pa = (c11 + c22) / 2.0 * 1e9
        C_2D = C_inplane_Pa * (h * 1e-10)          # Pa·m = N/m，用 h=V/A
        return C_2D, "N/m", "step6 面内(C11+C22)/2 × h(V/A)=%.3fÅ" % h
    else:
        if MANUAL["C_3D_GPa"]:
            return float(MANUAL["C_3D_GPa"]) * 1e9, "Pa", "manual"
        c11, c22 = read_elastic_inplane_GPa(cwd)
        if c11 is None:
            return None, "Pa", "缺 step6 弹性"
        return (c11 + c22) / 2.0 * 1e9, "Pa", "step6 (C11+C22)/2"


# ---------- m*：能带边抛物拟合（2D/3D 通用，全自动） ----------
def get_effective_mass(cwd, carrier, is_2d):
    """能带边抛物拟合 m*：E=C|Δk|² → m*/m0=3.80998/|C|（k 用倒格矢含2π，rad/Å）。
    2D 只取面内。返回 (m*/m0, provenance)；失败 (None, 原因)。"""
    key = "m_eff_electron" if carrier == "electron" else "m_eff_hole"
    if MANUAL[key]:
        return float(MANUAL[key]), "manual"
    vr = None
    for n in ("vasprun.xml", "vasprun.xml.gz"):
        p = Path(cwd) / UNIFORM_DIR / n
        if p.is_file():
            vr = p
            break
    if vr is None:
        return None, "缺 step3_uniform/vasprun.xml"
    try:
        import numpy as np
        from pymatgen.io.vasp import Vasprun
        v = Vasprun(str(vr), parse_dos=False, parse_potcar_file=False)
        recip = v.final_structure.lattice.reciprocal_lattice.matrix  # rad/Å, 含2π
        kfrac = np.array(v.actual_kpoints)                            # (nk,3)
        kcart = kfrac @ recip                                         # (nk,3) rad/Å
        hit = _find_band_edge(v, carrier)        # [PATCH-KE-2026] 跨自旋道
        if hit is None:
            return None, "无未占据/占据态 —— 体系是金属？（带边不存在，m* 无意义）"
        isp, k0, b0, ene = hit
        e0 = ene[k0, b0]
        eband = ene[:, b0]                        # 该带所有 k 的能量
        dk = kcart - kcart[k0]                    # 相对带边 Δk（rad/Å）
        de = eband - e0                           # ΔE（eV）
        # 2D：只取面内（|Δkz| 小），用 x,y 分量拟合
        if is_2d:
            zmask = np.abs(dk[:, 2]) < 0.02
            dk_use = dk[zmask][:, :2]
            de_use = de[zmask]
        else:
            dk_use = dk
            de_use = de
        q2 = np.sum(dk_use**2, axis=1)
        nz = q2 > 1e-9
        q2n, den = q2[nz], de_use[nz]
        if len(q2n) < 3:
            return None, "带边面内点太少（网格太粗）"
        # 先按半径逐步放宽找邻域；仍不够就退回"最近的若干点"，保证能拟合
        sel, Rused = None, None
        for R in (0.10, 0.15, 0.20, 0.30, 0.40, 0.50):
            m = q2n < R * R
            if np.count_nonzero(m) >= 3:
                sel, Rused = m, R
                break
        if sel is None:
            order = np.argsort(q2n)
            k = min(6, len(q2n))
            sel = np.zeros(len(q2n), bool)
            sel[order[:k]] = True
            Rused = float(np.sqrt(q2n[order[k - 1]]))
        C = np.sum(q2n[sel] * den[sel]) / np.sum(q2n[sel] ** 2)
        if abs(C) < 1e-6:
            return None, "曲率过小/拟合失败"
        m = 3.80998 / abs(C)
        return round(float(m), 4), "能带边抛物拟合(%s, %d点, R<%.2f, spin=%d)" % (
            "面内" if is_2d else "3D", int(np.count_nonzero(sel)), Rused, isp)
    except Exception as e:
        return None, "抛物拟合异常：%s" % type(e).__name__


# ---------- E1：从 deformation.h5 带边取（尽力） ----------
def _band_edges_json(cwd):
    """读 step7b_read 生成的 band_edges.json（amset 权威带边定位）。"""
    p = Path(cwd) / DEFORM_READ_DIR / "band_edges.json"
    if not p.is_file():
        return None
    try:
        be = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    # [SKILL_REV 交叉校验] band_edges.json 里的 rev 必须与本脚本一致，不一致说明
    # step7b 跑的是另一份 skill 副本（旧副本无 ionrelax 支持，会静默读刚性构型，
    # E1 偏低 20% 且无报错）。沿用 C2 哲学：不一致硬失败，不静默降级。
    be_rev = be.get("skill_rev")
    if be_rev and be_rev != _SKILL_REV:
        print("[WARN] band_edges.json skill_rev=%s 与本脚本 %s 不一致——"
              "step7b 可能跑的是旧副本（无 ionrelax 支持）"
              % (be_rev, _SKILL_REV))
    return be


def _vac_missing_msg(be, carrier, field):
    """[C2] E1_SOURCE='vac' 但字段缺失时的报错文案：带上生成端的跳过原因。

    step7b_read 会把真空对齐的结局写进 band_edges.json 的 vac_align：
      status=ok      → 字段应该在，缺了说明 json 被改过/口径不一致
      status=skipped → reason 就是根因（3D 无真空 / LOCPOT 缺失 / LVHAR 未开）
    没有 vac_align 说明 json 是旧版本，重跑 step7b_read。"""
    va = (be or {}).get("vac_align") or {}
    st = va.get("status")
    if st == "skipped":
        why = "step7b_read 跳过了真空对齐：%s" % va.get("reason", "未记录原因")
    elif st == "ok":
        why = "step7b_read 声称真空对齐成功，但 %s 缺失（json 不一致，重跑本步上游）" % field
    else:
        why = ("band_edges.json 无 vac_align 段（旧版本 step7b_read）——"
               "重跑 step7b_deform_read 生成新版")
    return ("E1_SOURCE='vac' 但 band_edges.json 无 %s：%s。"
            "看 step7_deform/band_edges.log；确认要用 amset 芯势口径就把 "
            "gen_step12_dpt.py 的 E1_SOURCE 改成 'amset'（数值口径不同，勿混report）"
            % (field, why))


def get_E1(cwd, carrier):
    """返回 (E1_eV, provenance)。尽力自动；失败返回 (None, 原因)。"""
    key = "E1_electron_eV" if carrier == "electron" else "E1_hole_eV"
    if MANUAL[key]:
        return float(MANUAL[key]), "manual"
    # [PATCH-DPT-2026] 优先 amset 权威带边（step7b_read 已定位好，k/band 全部对齐）
    be = _band_edges_json(cwd)
    if be and carrier in be and be[carrier].get("n_hits", 0) > 0:
        n = int(be[carrier]["n_hits"])
        # [PATCH-DPT-R6] 按 E1_SOURCE 选口径（vac=真空对齐/amset=平均芯势）。
        # [C2] 显式要了 vac 却没有 → 硬失败，不静默降级到 amset 口径。
        if E1_SOURCE == "vac":
            ev = be[carrier].get("E1_vac_iso_eV")
            if ev is None or abs(float(ev)) == 0:
                # [3D 兜底] 非 2D 体系无真空对齐，vac 口径本就不适用，
                # 自动用 amset 口径（E1_iso_eV，⟨|D|⟩），不属于静默降级。
                if _read_dim(cwd) != "2d":
                    _e1a = float(be[carrier].get("E1_iso_eV") or 0)
                    if _e1a > 0:
                        return round(abs(_e1a), 4), (
                            "band_edges.json 带边(amset 口径,%s 无真空)" % _read_dim(cwd))
                return None, _vac_missing_msg(be, carrier, "E1_vac_iso_eV")
            return round(abs(float(ev)), 4), (
                "band_edges.json 真空对齐(n=%d)面内对角" % n)
        e1 = float(be[carrier]["E1_iso_eV"])
        if e1 > 0:
            return round(abs(e1), 4), (      # [C5] 兜底 abs（amset h5 本身已非负）
                "band_edges.json 带边(简并平均,n=%d)面内对角" % n)
    h5 = Path(cwd) / DEFORM_READ_DIR / "deformation.h5"
    if not h5.is_file():
        return None, "缺 %s/deformation.h5 与 band_edges.json" % DEFORM_READ_DIR
    try:
        import numpy as np
        import h5py
        with h5py.File(str(h5), "r") as f:
            keys = _sorted_dp_keys(f)          # [PATCH-KE-2026] up 在前 down 在后
            if not keys:
                return None, "deformation.h5 无 deformation_potentials"
            probe = f[keys[0]]                 # (nband, nkpt, 3, 3) eV
            bk = _band_edge_index(cwd, carrier, probe.shape[0], probe.shape[1])
            isp = bk[2] if bk is not None else 0
            dset = keys[isp] if isp < len(keys) else keys[0]
            dp = np.array(f[dset])
        if bk is None:
            # [PATCH-DPT-R5] 不再静默走「全带中位数」兜底（那是本次修复的 bug）：
            # 带边定位失败就显式失败，提示修 step7b 或手填 MANUAL_ANISO。
            return None, "带边定位失败：请检查 step7b_read 的 band_edges.json，" \
                         "或手填 MANUAL/MANUAL_ANISO 的 E1"
        b, k = bk[0], bk[1]
        e1 = abs((dp[b, k, 0, 0] + dp[b, k, 1, 1]) / 2.0)   # 面内对角均值 |eV|
        return round(float(e1), 4), (
            "deformation.h5 带边(b=%d,k=%d,%s)面内对角(旧逻辑)" % (b, k, dset))
    except Exception as e:
        return None, "读 deformation.h5 异常：%s" % type(e).__name__


def _band_edge_index(cwd, carrier, nband, nkpt):
    """用 step3 vasprun 定位 VBM/CBM，返回 (band_idx, kpt_idx, spin_idx)。
    [PATCH-KE-2026] 增加第三个返回值 spin_idx，供选 deformation.h5 的 up/down 数据集。
    失败返回 None。"""
    try:
        import numpy as np
        from pymatgen.io.vasp import Vasprun
        vr = None
        for n in ("vasprun.xml", "vasprun.xml.gz"):
            p = Path(cwd) / UNIFORM_DIR / n
            if p.is_file():
                vr = Vasprun(str(p), parse_dos=False, parse_potcar_file=False)
                break
        if vr is None:
            return None
        hit = _find_band_edge(vr, carrier)          # [PATCH-KE-2026] 跨自旋道
        if hit is None:
            return None
        isp, k, b, _ene = hit
        if b < nband and k < nkpt:
            return int(b), int(k), int(isp)
        return None
    except Exception:
        return None


# ---------- DPT 迁移率公式 ----------
def mobility_dpt(is_2d, C, m_star, E1_eV, T):
    """返回迁移率 cm²/(V·s)。C：2D=N/m，3D=Pa。m_star：m0 倍数。"""
    m = m_star * M0
    E1 = E1_eV * E_C
    if is_2d:
        mu = (E_C * HBAR**3 * C) / (KB * T * m * m * E1**2)     # m²/(V·s)
    else:
        mu = (2.0 * math.sqrt(2 * math.pi) * E_C * HBAR**4 * C) \
             / (3.0 * (KB * T)**1.5 * m**2.5 * E1**2)
    return mu * 1e4                                            # -> cm²/(V·s)


# === patch_dpt_aniso：各向异性 DPT（m*、E1、C 全部分方向）===
MANUAL_ANISO = {          # 分方向手填覆盖，None=自动。x/y 为笛卡尔面内方向
    "m_eff_electron_xy": None,    # (m_x, m_y)，单位 m0，例 (0.98, 0.90)
    "m_eff_hole_xy": None,
    "E1_electron_xy": None,       # (E1_x, E1_y)，eV
    "E1_hole_xy": None,
    "C_2D_xy_N_per_m": None,      # (C_x, C_y)，N/m
}


def get_C_aniso(cwd, is_2d):
    """分方向 2D 刚度 (C_x, C_y) = (C11, C22) × h(V/A)，N/m。"""
    if MANUAL_ANISO["C_2D_xy_N_per_m"]:
        cx, cy = MANUAL_ANISO["C_2D_xy_N_per_m"]
        return (float(cx), float(cy)), "manual"
    c11, c22 = read_elastic_inplane_GPa(cwd)
    if c11 is None or c22 is None:
        return None, "缺 step6 弹性"
    h = _cell_height_A(cwd)
    if not h:
        return None, "缺元胞高度 h=V/A"
    f = 1e9 * (h * 1e-10)
    return (c11 * f, c22 * f), "step6 C11/C22 × h(V/A)=%.3fA" % h


def get_effective_mass_aniso(cwd, carrier, is_2d):
    """带边二次型拟合 ΔE = a·Δkx² + b·Δky² + c·Δkx·Δky + d·Δkx + e·Δky + f。

    [P2] 分方向质量 = 逆质量张量 H⁻¹ 的对角元（H=∂²E/∂k∂k=[[2a,c],[c,2b]]）：
         m_x/m0 = 3.80998·4b/(4ab-c²)，m_y/m0 = 3.80998·4a/(4ab-c²)。
         c=0 退化回 m_x=3.80998/|a|、m_y=3.80998/|b|（注意 m_x 由 b 决定）。
    [P1] 线性项 d/e/f 吸收 k0 不在真实极值点的偏移；加 cond(A)+相对残差验收，
         病态/残差大就报错，绝不静默返回一个错的 m*。
    返回 ((m_x, m_y), provenance)；失败 (None, 原因)。"""
    key = "m_eff_electron_xy" if carrier == "electron" else "m_eff_hole_xy"
    if MANUAL_ANISO[key]:
        mx, my = MANUAL_ANISO[key]
        return (float(mx), float(my)), "manual"
    if not is_2d:
        return None, "各向异性 m* 目前只实现 2D 面内"
    vr = None
    for n in ("vasprun.xml", "vasprun.xml.gz"):
        p = Path(cwd) / UNIFORM_DIR / n
        if p.is_file():
            vr = p
            break
    if vr is None:
        return None, "缺 step3_uniform/vasprun.xml"
    try:
        import numpy as np
        from pymatgen.io.vasp import Vasprun
        v = Vasprun(str(vr), parse_dos=False, parse_potcar_file=False)
        recip = v.final_structure.lattice.reciprocal_lattice.matrix
        # [CELL 取向] 正交胞（90°）且长轴在 b 时，by_direction 的 x/y 相对文献
        # 转置（数值全对结论全反）。SS/LS 需 a=长轴(调制方向)在 x。
        _lat = v.final_structure.lattice
        if (abs(_lat.alpha - 90) < 1 and abs(_lat.beta - 90) < 1
                and abs(_lat.gamma - 90) < 1 and _lat.b > _lat.a * 1.2):
            print("[WARN] 胞 a=%.2f < b=%.2f：面内长轴不在 x 上——若本体系有"
                  "面内各向异性，by_direction 的 x/y 可能相对文献转置"
                  "（查 step.conf 的 CELL_POLICY）" % (_lat.a, _lat.b))
        kfrac_ibz = np.array(v.actual_kpoints)      # IBZ (nk,3)
        hit = _find_band_edge(v, carrier)        # [PATCH-KE-2026] 跨自旋道
        if hit is None:
            return None, "无未占据/占据态 —— 体系是金属？（带边不存在，m* 无意义）"
        isp, k0_ibz, b0, ene_ibz = hit           # ene_ibz (nk, nb)

        # [交叉校验] m* 链（本步 step3_uniform）与 E1 链（step7 undeformed，
        # band_edges.json 里的 k_frac）必须定位到同一个带边 k 点；不一致说明
        # 两链各自独立定位时在某个体系上分了叉，m* 与 E1 内部不自洽（照样不报错）。
        _be = _band_edges_json(cwd)
        if _be and carrier in _be:
            _hits = _be[carrier].get("hits") or []
            if _hits and _hits[0].get("k_frac") is not None:
                _kf_be = np.array(_hits[0]["k_frac"], dtype=float)
                _kf_m = kfrac_ibz[k0_ibz].astype(float)
                _d = _kf_be - _kf_m
                _d -= np.round(_d)                   # 周期回卷
                if float(np.linalg.norm(_d)) > 1e-4:
                    print("[WARN] %s：m* 链带边 k=%s 与 E1 链带边 k=%s 不一致"
                          "（回卷距离 %.3g）——两链带边分叉，m* 与 E1 内部不自洽"
                          % (carrier, np.round(_kf_m, 4).tolist(),
                             np.round(_kf_be, 4).tolist(), float(np.linalg.norm(_d))))

        # [P1-2] 展开 IBZ → 全 BZ：带边 K 点坐在 IBZ 楔形边界，近邻点只存在于
        # 楔形内一侧，单侧取点会让 a/b 两个曲率被不对称地吃掉（hex 实测 2.7 倍差）。
        # 展开后 k0 周围点对称，二次型拟合才无偏。失败退回 IBZ（靠 cond/残差验收兜底）。
        # [TR] 时间反演只在净磁矩≈0 时是能带的对称操作。CrS2/CrSe2 塌到零磁矩
        # 无碍，但本 skill 还要复用到 MnIn2Se4/Mn2In2Se5 等磁性体系——自旋分辨
        # 能带在有净磁矩时 TR 会把 up/down 错配。有磁矩就关掉 TR。
        # [TR] 时间反演只在净磁矩≈0 时是能带的对称操作。ISPIN=1 → TR 安全；
        # ISPIN=2 → 读 OUTCAR 总磁矩判断，读不到 → 保守关掉（磁性体系不安全）。
        use_tr = True
        try:
            ispin = int(v.parameters.get("ISPIN", 1))
            if ispin == 1:
                use_tr = True
            else:
                oc = Path(cwd) / UNIFORM_DIR / "OUTCAR"
                tot_mag = None
                if oc.is_file():
                    import re as _re
                    m = _re.findall(r"number of electron\s+\S+\s+magnetization\s+(\S+)",
                                    oc.read_text(errors="ignore"))
                    if m:
                        tot_mag = abs(float(m[-1]))
                if tot_mag is None:
                    use_tr = False
                    print("[WARN] %s：ISPIN=2 但读不到总磁矩（OUTCAR 缺失/解析失败），"
                          "保守关闭 time_reversal" % carrier)
                else:
                    use_tr = (tot_mag <= 0.05)
        except Exception:
            use_tr = True

        # [P1-2] 展开 IBZ → 全 BZ，两层：amset（环境）→ pymatgen 点群（不依赖环境）。
        # 【彻底删掉 IBZ 兜底】IBZ 上的 m* 不是"精度差一点"，是系统性错误（单侧
        # 取点，SS 正交胞对称性禁止交叉项却拟合出 off=0.28 的假数）。展开不成功
        # 就硬失败、不出 m*，绝不静默退回 IBZ。
        expanded = False
        _expand_method = None
        try:
            from amset.electronic_structure.symmetry import expand_kpoints
            full_kfrac, _, _, _, _, kp_mapping = expand_kpoints(
                v.final_structure, kfrac_ibz, symprec=0.01,
                time_reversal=use_tr, return_mapping=True)
            kp_mapping = np.asarray(kp_mapping)
            kfrac = np.array(full_kfrac)            # (n_full, 3)
            _expand_method = "amset"
            expanded = True
        except Exception:
            # 层2：subprocess 到 step.conf 的 amset 环境跑 expand_kpoints
            # （jzzn 默认 python 是 atomate2_p_a 无 amset，但 conda 里有 amset_clean）
            _sub_ok = False
            try:
                import subprocess as _sp, shlex as _shlex, json as _json
                _env = None
                try:
                    import stepconf as _sc
                    _txt = open(_sc.CONF_NAME, encoding="utf-8-sig").read()
                    _p = {k.upper(): v for k, v, _ in _sc.parse(_txt, _sc.CONF_NAME).get("params", [])}
                    _sh, _e = _p.get("CONDA_SH"), _p.get("AMSET_ENV")
                    if _sh and _e:
                        _env = "source %s && conda activate %s" % (_sh, _e)
                except Exception:
                    pass
                if _env:
                    _code = ("import sys,json,numpy as np\n"
                             "from pymatgen.core import Structure\n"
                             "from amset.electronic_structure.symmetry import expand_kpoints\n"
                             "st=Structure.from_dict(json.loads(sys.argv[1]))\n"
                             "kf=np.array(json.loads(sys.argv[2]))\n"
                             "ut=sys.argv[3]=='True'\n"
                             "f,_,_,_,_,kp=expand_kpoints(st,kf,symprec=0.01,time_reversal=ut,return_mapping=True)\n"
                             "print(json.dumps({'full':np.array(f).tolist(),'kp':np.asarray(kp).tolist()}))")
                    _cmd = "%s && python3 -c %s %s %s %s" % (
                        _env, _shlex.quote(_code),
                        _shlex.quote(_json.dumps(v.final_structure.as_dict())),
                        _shlex.quote(_json.dumps(kfrac_ibz.tolist())),
                        str(use_tr))
                    _out = _sp.run(["bash", "-lc", _cmd], capture_output=True,
                                   text=True, timeout=180)
                    _lines = [l for l in (_out.stdout or "").strip().splitlines()
                              if l.strip().startswith("{")]
                    if _lines:
                        _data = _json.loads(_lines[-1])
                        kp_mapping = np.asarray(_data["kp"])
                        kfrac = np.array(_data["full"])
                        _expand_method = "amset-subproc"
                        expanded = True
                        _sub_ok = True
            except Exception:
                pass
            if not _sub_ok:
                # 层3：pymatgen 点群展开（+ 时间反演，只依赖 pymatgen）
                try:
                    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
                    _ops = SpacegroupAnalyzer(
                        v.final_structure, symprec=0.01
                    ).get_point_group_operations(cartesian=False)
                    _seen, _kf_list, _idx_list = {}, [], []
                    for _i, _k in enumerate(kfrac_ibz):
                        _targets = [op.operate(_k) for op in _ops]
                        if use_tr:
                            _targets += [op.operate(-_k) for op in _ops]
                        for _kk in _targets:
                            _kk -= np.round(_kk)
                            _key = tuple(np.round(_kk, 6))
                            if _key not in _seen:
                                _seen[_key] = True
                                _kf_list.append(_kk)
                                _idx_list.append(_i)
                    if not _kf_list:
                        raise RuntimeError("点群展开得到 0 个点")
                    # [网格一致性校验] 与 amset 同等严格：铺满才接受，否则单侧取点
                    _df = kfrac_ibz - kfrac_ibz[0]; _df -= np.round(_df)
                    _nz = np.abs(_df) > 1e-6
                    _mesh = np.array([int(round(1.0 / float(np.min(np.abs(_df[_nz[:, i], i])))))
                                      if _nz[:, i].any() else 1 for i in range(3)])
                    _n_expect = int(np.prod(_mesh))
                    if len(_kf_list) != _n_expect:
                        raise RuntimeError(
                            "pymatgen 展开只得到 %d/%d 点，网格未铺满"
                            % (len(_kf_list), _n_expect))
                    kfrac = np.array(_kf_list)
                    kp_mapping = np.asarray(_idx_list)
                    _expand_method = "pymatgen"
                    expanded = True
                except Exception as _e2:
                    return None, ("全 BZ 展开失败（amset/subprocess/pymatgen 都不可用：%s）"
                                  "——拒绝在 IBZ 上拟合 m*（单侧取点是系统性错误）"
                                  % type(_e2).__name__)
        # 展开后（amset/pymatgen 共用）：能量映射 + 精确匹配 IBZ 原 k
        ene = ene_ibz[kp_mapping]                   # (n_full, nb)
        d = kfrac - kfrac_ibz[k0_ibz]; d -= np.round(d)
        dd = np.linalg.norm(d, axis=1)
        k0 = int(np.argmin(dd))
        if dd[k0] > 1e-4:
            return None, "展开后找不到原带边 k 点（回卷距离 %.3g）" % dd[k0]

        kcart = kfrac @ recip
        dk = kcart - kcart[k0]
        de = ene[:, b0] - ene[k0, b0]
        zm = np.abs(dk[:, 2]) < 0.02                 # 只取面内
        dkx, dky, dee = dk[zm][:, 0], dk[zm][:, 1], de[zm]
        q2 = dkx ** 2 + dky ** 2
        qnz = np.sqrt(q2[q2 > 1e-9])
        if len(qnz) == 0:
            return None, "带边面内无有效 k 点（q > 0）"
        q_min = float(qnz.min())
        # [SHELL+AXIS] 网格分割数：从全 BZ k 点反解。半径由「最差采样轴」的笛卡尔
        # 步长定，细长胞（LS a=21.64, N_x≈2）不会圈出单方向长条。壳层用相对距离
        # q/q_min 聚类（容差 1e-3），避免浮点噪声把单壳层劈成两个。
        try:
            from amset.electronic_structure.symmetry import get_mesh_from_kpoint_diff
            _mesh, _ = get_mesh_from_kpoint_diff(kfrac)
            mesh = np.rint(np.asarray(_mesh)).astype(int)
        except Exception:
            _df = kfrac - kfrac[k0]; _df -= np.round(_df)
            _nz = np.abs(_df) > 1e-6
            mesh = np.array([int(round(1.0 / float(np.min(np.abs(_df[_nz[:, i], i])))))
                             if _nz[:, i].any() else 1 for i in range(3)])
        step_cart = np.array([np.linalg.norm(recip[i]) / max(int(mesh[i]), 1)
                              for i in (0, 1)])
        # [SS/LS 线性项] 带边非高对称点（Y-Γ 路径，如 SS k=(0,0.3636)）时，真实
        # 极值点一般不在网格点上，存在真实线性项——继续升 NSTEP 直到点数够 3 倍
        # 冗余（18 点），否则线性项被关掉，k0 偏移被 a/b 吸收（重演 CrS2 曲率偏置，
        # 且这次没有对称性兜底）。
        def _is_high_sym(kf, tol=1e-3):
            _bases = (0.0, 0.5, 1.0 / 3.0, 2.0 / 3.0, 0.25, 0.75)
            for _c in kf[:2]:
                _c = _c % 1.0
                if not any(abs(_c - _b) < tol or abs(_c - _b - 1.0) < tol
                           for _b in _bases):
                    return False
            return True
        need_lin = not _is_high_sym(kfrac[k0])

        sel = Rused = n_shell = NSTEP_used = None
        _nstep_iter = (FORCE_NSTEP,) if FORCE_NSTEP else (2, 3, 4, 5)
        for NSTEP in _nstep_iter:
            # -0.1 留余量：整数步恰好卡在下一壳层边界，浮点噪声会把下一壳层的
            # 个别点纳入。1.9 步≈0.302 落在第二/三壳层之间。
            Rmax = (NSTEP - 0.1) * float(step_cart.min())
            m = (q2 < Rmax * Rmax) & (q2 > 1e-9)
            n = int(np.count_nonzero(m))
            if n < 8:
                continue
            q_sel = np.sqrt(q2[m])
            shells = np.unique(np.round(q_sel / q_min, 3))
            n_sh = int(len(shells))
            ok = (n_sh >= 2)                        # 至少 2 壳层，才有径向信息
            if need_lin:
                ok = ok and (n >= 18)               # 非高对称点：线性项需 3 倍冗余
            if ok:
                sel, Rused, n_shell, NSTEP_used = m, Rmax, n_sh, NSTEP
                break
        if sel is None:
            return None, ("带边面内点太少或全在单壳层（需 ≥8 点 + ≥2 壳层）——"
                          "加密 step3_uniform 网格或手填 MANUAL_ANISO")
        npt = int(np.count_nonzero(sel))

        # [AXIS 覆盖] 每个面内倒格矢方向都要有非零步的点，否则该方向曲率无约束。
        # LS 的 kx 轴只有 N≈2 分割，会在这里干脆报错而不是返回假 m*_x。
        _dfrac = kfrac[sel] - kfrac[k0]
        _dfrac -= np.round(_dfrac)
        _steps = np.rint(_dfrac * mesh).astype(int)
        for _i in (0, 1):
            if int(np.abs(_steps[:, _i]).max()) < 1:
                return None, ("倒格矢方向 %d 无非零步取点（该轴分割数 N=%d 过少），"
                              "该方向曲率完全无约束——把 step3_uniform 的 KSPACING "
                              "调小或设 KMIN_DIV≥12" % (_i, int(mesh[_i])))
        # [P1] 设计矩阵：高对称点（K）在网格上时线性项被 C₃ 强制为 0，但 SS/LS
        # 的带边在 Y–Γ 路径上（非高对称点），真实极值点一般不在网格点上，存在
        # 真实线性项——点数够就拟合它，吸收 k0 偏移；否则只拟合二次项。
        # [Q4] ≥2 壳层时再加各向同性四次项 q⁴，把次抛物偏置直接拟掉。
        use_quartic = (n_shell >= 2) and (npt >= 8)
        # 线性项（d,e,f）需 3 倍冗余才稳定：2 倍冗余（14 点）在对称点上会把曲率
        # 拟合进线性项（hex 实测 m* 1.124 vs 0.924）。≥18 点才启用。
        use_linear = (npt >= 18)
        cols = [dkx[sel] ** 2, dky[sel] ** 2, dkx[sel] * dky[sel]]
        names = ["a", "b", "c"]
        if use_quartic:
            cols.append(q2[sel] ** 2)
            names.append("q4")
        if use_linear:
            cols += [dkx[sel], dky[sel], np.ones(npt)]
            names += ["d", "e", "f"]
        A = np.column_stack(cols)
        if A.shape[0] < 2 * A.shape[1]:
            return None, ("拟合点数不足：%d 点 / %d 参数（需 ≥2 倍冗余）——"
                          "加密 step3_uniform 网格" % (A.shape[0], A.shape[1]))
        cond = float(np.linalg.cond(A))
        if cond > 1e5:
            return None, ("设计矩阵病态 cond=%.1e（k 点单侧/共线，"
                          "IBZ 楔形取点不对称）" % cond)
        sol, *_ = np.linalg.lstsq(A, dee[sel], rcond=None)
        a, b, c = float(sol[0]), float(sol[1]), float(sol[2])
        pred = A @ sol
        rel = float(np.linalg.norm(dee[sel] - pred)
                    / (np.linalg.norm(dee[sel]) + 1e-12))
        if rel > 0.2:
            return None, "二次型拟合残差过大 rel=%.2f（带边附近非二次型）" % rel
        # [P2] 逆质量张量求逆的对角元（H=[[2a,c],[c,2b]]）。
        # 质量恒正：electron(CBM) 曲率 a,b>0，hole(VBM) 曲率 a,b<0——统一取 |a|,|b|，
        # 否则 hole 会得到负质量（den=4ab-c² 因 ab>0 为正，但分子 4b/4a 带负号）。
        if a * b <= 0:
            return None, ("带边曲率异号（a=%.3f b=%.3f，鞍点？"
                          "带边定位可疑或取点跨了多个带边）" % (a, b))
        aa, bb = abs(a), abs(b)
        den = 4.0 * aa * bb - c * c
        if den <= 1e-12:
            return None, "逆质量张量奇异（4|a||b|-c²=%.2g，能量椭圆退化）" % den
        mx = 3.80998 * (4.0 * bb) / den
        my = 3.80998 * (4.0 * aa) / den
        if mx <= 0 or my <= 0:
            return None, "质量为负（mx=%.3f my=%.3f）" % (mx, my)
        off = abs(c) / max(abs(a), abs(b))
        prov = ("带边二次型拟合(%s, %d点/%d壳层, R<%.2f, 模型=%s, "
                "交叉项/主项=%.2f, cond=%.0e, rel=%.3f, spin=%d)"
                % (_expand_method, npt, n_shell, Rused,
                   "+".join(names), off, cond, rel, isp))
        # [P2 后] 交叉项判据：正交胞（SS/LS/*_ortho）的点群禁止面内交叉项，
        # off 明显非零 = 拟合本身有问题（取点不对称/带边非极值点/跨带），
        # 不再是"手填 MANUAL_ANISO 就能绕过"的表象问题。
        if off > 0.3:
            print("[WARN] %s：m* 拟合交叉项/主项=%.2f 偏大 —— 六方胞里 C₃ 强制"
                  "各向同性、正交胞里对称性禁止面内交叉项，两种情况下 off 都应"
                  "接近 0；非零说明拟合有问题（取点不对称/带边不是真极值点/"
                  "取点跨了多条带），此时分方向 m* 与由它导出的 μ、τ 都不可信。"
                  % (carrier, off))
        return (round(mx, 4), round(my, 4)), prov
    except Exception as e:
        return None, "二次型拟合异常：%s" % type(e).__name__


def get_E1_aniso(cwd, carrier):
    """带边形变势 (|D_xx|, |D_yy|)，eV —— 不再取面内平均。"""
    key = "E1_electron_xy" if carrier == "electron" else "E1_hole_xy"
    if MANUAL_ANISO[key]:
        ex, ey = MANUAL_ANISO[key]
        return (float(ex), float(ey)), "manual"
    # [PATCH-DPT-2026] 优先 amset 权威带边（step7b_read 已定位好，k/band 全部对齐）
    be = _band_edges_json(cwd)
    if be and carrier in be and be[carrier].get("n_hits", 0) > 0:
        n = int(be[carrier]["n_hits"])
        # [PATCH-DPT-R6] 按 E1_SOURCE 选口径。[C5] 两套字段生成端都已取 |·|，
        # 这里的 abs() 只是兜底。[C2] 要了 vac 却没有 → 硬失败，不降级。
        if E1_SOURCE == "vac":
            vx = be[carrier].get("E1_vac_xx_eV")
            vy = be[carrier].get("E1_vac_yy_eV")
            if (vx is None or vy is None
                    or abs(float(vx)) == 0 or abs(float(vy)) == 0):
                return None, _vac_missing_msg(be, carrier,
                                              "E1_vac_xx_eV/E1_vac_yy_eV")
            return ((round(abs(float(vx)), 4), round(abs(float(vy)), 4)),
                    "band_edges.json 真空对齐(n=%d) D_xx/D_yy" % n)
        ex = float(be[carrier]["E1_xx_eV"])
        ey = float(be[carrier]["E1_yy_eV"])
        if ex > 0 and ey > 0:
            return ((round(abs(ex), 4), round(abs(ey), 4)),   # [C5] 兜底 abs
                    "band_edges.json 带边(简并平均,n=%d) D_xx/D_yy" % n)
    h5 = Path(cwd) / DEFORM_READ_DIR / "deformation.h5"
    if not h5.is_file():
        return None, "缺 %s/deformation.h5 与 band_edges.json" % DEFORM_READ_DIR
    try:
        import numpy as np
        import h5py
        with h5py.File(str(h5), "r") as f:
            keys = _sorted_dp_keys(f)          # [PATCH-KE-2026] up 在前 down 在后
            if not keys:
                return None, "deformation.h5 无 deformation_potentials"
            probe = f[keys[0]]
            bk = _band_edge_index(cwd, carrier, probe.shape[0], probe.shape[1])
            isp = bk[2] if bk is not None else 0
            dset = keys[isp] if isp < len(keys) else keys[0]
            dp = np.array(f[dset])
        if bk is None:
            return None, "带边定位失败：请检查 step7b_read 的 band_edges.json，" \
                         "或手填 MANUAL_ANISO 的 E1"
        b, k = bk[0], bk[1]
        return ((round(float(abs(dp[b, k, 0, 0])), 4),
                 round(float(abs(dp[b, k, 1, 1])), 4)),
                "deformation.h5 带边(b=%d,k=%d,%s) D_xx/D_yy(旧逻辑)" % (b, k, dset))
    except Exception as e:
        return None, "读 deformation.h5 异常：%s" % type(e).__name__


def mobility_dpt_2d_aniso(C_alpha, m_alpha, m_d, E1_eV, T):
    """μ_α = eℏ³C_2D,α/(k_B T m*_α m_d E1_α²)，cm²/(V·s)。m_d=√(m_x m_y)。"""
    E1 = E1_eV * E_C
    mu = (E_C * HBAR ** 3 * C_alpha) / (KB * T * (m_alpha * M0) * (m_d * M0) * E1 ** 2)
    return mu * 1e4


def _aniso_block(cwd, is_2d, carrier, T):
    """分方向 DPT 结果块。3D 不走这条分支。"""
    if not is_2d:
        return {"status": "3D 体系不做各向异性分支，用上面的各向同性值"}
    Cxy, Cprov = get_C_aniso(cwd, is_2d)
    mxy, mprov = get_effective_mass_aniso(cwd, carrier, is_2d)
    exy, eprov = get_E1_aniso(cwd, carrier)
    blk = {"C_2D_N_per_m": Cxy, "C_provenance": Cprov,
           "m_eff_m0": mxy, "m_provenance": mprov,
           "E1_eV": exy, "E1_provenance": eprov, "T_K": T,
           "formula": "mu_a = e*hbar^3*C_2D,a/(kB*T*m_a*m_d*E1_a^2); tau_a = mu_a*m_a/e"}
    miss = [n for n, v in (("C", Cxy), ("m*", mxy), ("E1", exy)) if v is None]
    if miss:
        blk["status"] = "缺输入：%s（可在 MANUAL_ANISO 手填）" % "、".join(miss)
        return blk
    m_d = math.sqrt(mxy[0] * mxy[1])
    blk["m_d_m0"] = round(m_d, 4)
    for i, d in enumerate(("x", "y")):
        mu = mobility_dpt_2d_aniso(Cxy[i], mxy[i], m_d, exy[i], T)
        blk[d] = {"C_2D_N_per_m": round(Cxy[i], 3), "m_eff_m0": mxy[i],
                  "E1_eV": exy[i], "mobility_cm2_Vs": round(mu, 3),
                  "tau_s": (mu * 1e-4) * (mxy[i] * M0) / E_C}
    blk["status"] = "ok"
    return blk


def _one_carrier(cwd, is_2d, carrier, T):
    C, Cunit, Cprov = get_C(cwd, is_2d)
    m, mprov = get_effective_mass(cwd, carrier, is_2d)
    e1, e1prov = get_E1(cwd, carrier)
    rec = {"carrier": carrier,
           "inputs": {"C_%s" % ("2D_N_per_m" if is_2d else "3D_Pa"): C,
                      "C_provenance": Cprov,
                      "m_eff_m0": m, "m_provenance": mprov,
                      "E1_eV": e1, "E1_provenance": e1prov,
                      "T_K": T}}
    if None in (C, m, e1):
        miss = [n for n, v in (("C", C), ("m*", m), ("E1", e1)) if v is None]
        rec["mobility_cm2_Vs"] = None
        rec["status"] = "缺输入：%s —— 在脚本顶部 MANUAL 手填后重跑（μ∝1/T，报 %.0fK）" \
                        % ("、".join(miss), T)
    else:
        rec["mobility_cm2_Vs"] = round(mobility_dpt(is_2d, C, m, e1, T), 3)
        rec["status"] = "ok"
    rec["by_direction"] = _aniso_block(cwd, is_2d, carrier, T)   # patch_dpt_aniso
    return rec


def main():
    cwd = Path.cwd()
    _guard_not_0d(cwd)
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)
    dim = _read_dim(cwd)
    is_2d = (dim == "2d")

    carriers = (["electron", "hole"] if CARRIER == "both" else [CARRIER])
    # [ENV] 记录执行环境，铺开时"哪个环境跑的"从推断变成可读事实
    try:
        import socket as _socket
        _amset_ver = "N/A"
        try:
            import amset as _amset
            _amset_ver = getattr(_amset, "__version__", "N/A")
        except Exception:
            pass
        _env = {"python": sys.executable, "amset": _amset_ver,
                "host": _socket.gethostname()}
    except Exception:
        _env = {"python": sys.executable}
    res = {"dim": dim, "is_2d": is_2d, "temperature_K": TEMPERATURE_K,
           "skill_rev": _SKILL_REV, "env": _env,
           "formula": ("2D Bardeen-Shockley: μ=eℏ³C_2D/(k_BT m* m_d E1²)" if is_2d
                       else "3D: μ=2√(2π)eℏ⁴C_3D/(3(k_BT)^{3/2}m*^{5/2}E1²)"),
           "results": [_one_carrier(cwd, is_2d, c, TEMPERATURE_K) for c in carriers],
           "note": ("经典 DPT 声学支迁移率，用于和 amset(ADP) 对标。m*/E1 自动值精度"
                    "有限——务必核对，或在 MANUAL 手填。μ∝1/T。")}
    (out / "dpt_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# DPT 迁移率摘要（%s, %.0f K）" % (dim, TEMPERATURE_K)]
    for r in res["results"]:
        mu = r["mobility_cm2_Vs"]
        lines.append("%-9s μ = %s cm²/(V·s)   [%s]" % (
            r["carrier"], (("%.1f" % mu) if mu is not None else "—"), r["status"]))
        i = r["inputs"]
        lines.append("           C=%s(%s)  m*=%s(%s)  E1=%s eV(%s)" % (
            i.get("C_2D_N_per_m", i.get("C_3D_Pa")), i["C_provenance"],
            i["m_eff_m0"], i["m_provenance"], i["E1_eV"], i["E1_provenance"]))
        bd = r.get("by_direction") or {}                      # patch_dpt_aniso
        if bd.get("status") == "ok":
            for d in ("x", "y"):
                q = bd[d]
                lines.append("           [%s] mu=%.1f cm2/Vs  tau=%.1f fs  "
                             "m*=%.3f  E1=%.3f eV  C_2D=%.1f N/m" % (
                                 d, q["mobility_cm2_Vs"], q["tau_s"] * 1e15,
                                 q["m_eff_m0"], q["E1_eV"], q["C_2D_N_per_m"]))
            lines.append("           m_d=%.3f  [%s | %s]" % (
                bd["m_d_m0"], bd["m_provenance"], bd["E1_provenance"]))
        elif bd.get("status"):
            lines.append("           [各向异性] %s" % bd["status"])
    (out / "dpt_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 软失败 → 报错：关键产物(迁移率)一个都算不出，就让 tf 判 error 而非 completed，
    # 并指明缺什么、该跑哪一步（不手填、全自动的前提下）。
    if all(r["mobility_cm2_Vs"] is None for r in res["results"]):
        miss = set()
        for r in res["results"]:
            i = r["inputs"]
            if i.get("C_2D_N_per_m") is None and i.get("C_3D_Pa") is None:
                miss.add("C←S6_elastic")
            if i["m_eff_m0"] is None:
                miss.add("m*←S3_uniform(vasprun/网格)")
            if i["E1_eV"] is None:
                miss.add("E1←S7.1_read(deformation.h5)")
        hint = "；".join(sorted(miss)) or "见 json 的 provenance"
        sys.exit("[ERROR] DPT 迁移率全部未算出。缺：%s。请先把对应步骤跑好/修好再重跑本步"
                 "（dpt_result.json 已写出，含各输入来源可排查）。" % hint)

    print("[DONE] %s：dpt_result.json 已生成" % OUTDIR_NAME)


if __name__ == "__main__":
    main()