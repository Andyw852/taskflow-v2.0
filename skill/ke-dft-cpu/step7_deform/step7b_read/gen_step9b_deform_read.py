#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step7b_deform_read.py —— amset deform read → deformation.h5（step7b_deform_read）。

run:gen 步骤：在登录节点直接跑，不提交 SLURM。
把 step7_deform 的全部形变单点结果读进 deformation.h5。秒级完成。
产出：本步目录下的 deformation.h5（done_marker）。
"""
import glob
import os
import shlex
import subprocess
import sys
from pathlib import Path

# =========================== 可改参数区 ===========================
# [SKILL_REV] 版本戳：写进 band_edges.json，与 gen_step12_dpt.py 交叉校验一致。
_SKILL_REV = "2026-08-29-ionrelax-nstep"
OUTDIR_NAME  = "step7b_deform_read"
DEFORM_DIR   = "step7_deform"
# patch_deform_fix：只匹配形变目录。绝不能用 "*deform*"——它会把
# "undeformed" 自己也匹配进去，undeformed 必须单独作为 bulk 传给 read。
DEFORM_GLOB  = "deform-*"
# amset 环境：优先读 step.conf 里 tf 注入的集群 CONDA_SH/AMSET_ENV（setting/<集群>.yaml
# 里配 conda_sh/amset_env，每人按自己的机器配置，脚本不硬编码路径）；
# step.conf 没有（旧项目/直跑脚本）才回退主机探测。
import os as _os
def _amset_env_src():
    try:
        import stepconf as _sc
        _txt = open(_sc.CONF_NAME, encoding="utf-8-sig").read()
        _p = {k.upper(): v for k, v, _ in _sc.parse(_txt, _sc.CONF_NAME).get("params", [])}
        _sh, _env = _p.get("CONDA_SH"), _p.get("AMSET_ENV")
        if _sh and _env:
            return "source %s && conda activate %s" % (_sh, _env)
    except Exception:
        pass
    if _os.path.isdir("/home/wangchaoyue852/miniconda3"):
        return "source /home/wangchaoyue852/miniconda3/etc/profile.d/conda.sh && conda activate amset"
    return "source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh && conda activate amset_clean"
AMSET_ENV_SRC = _amset_env_src()
# =================================================================


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
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)
    _guard_not_0d(cwd, "step7b_deform_read",
                  "形变势是能带对应变的响应，孤立分子没有能带色散")
    dfm = cwd / DEFORM_DIR
    if not dfm.is_dir():
        sys.exit("[ERROR] 找不到 %s（形变单点没生成？）" % dfm)

    # 校验每个形变子目录都有 vasprun.xml，否则 read 会失败或给错结果
    subs = sorted(p for p in glob.glob(str(dfm / DEFORM_GLOB)) if os.path.isdir(p))
    und = str(dfm / "undeformed")
    if not os.path.isdir(und):
        sys.exit("[ERROR] 缺 %s —— 它是形变势的参考态，没有它 read 无法对齐"
                 % und)
    subs.append(und)
    if not [p for p in subs if p != und]:
        sys.exit("[ERROR] %s 下没有 deform-* 子目录（step7 没跑或跑挂了）" % dfm)
    missing = [os.path.basename(p) for p in subs
               if not (os.path.isfile(os.path.join(p, "vasprun.xml"))
                       or os.path.isfile(os.path.join(p, "vasprun.xml.gz")))]
    if missing:
        sys.exit("[ERROR] 以下形变单点还没算完（缺 vasprun.xml）：%s\n"
                 "        等 step7_deform 全部 done 再跑本步。"
                 % ", ".join(missing[:8]))

    # amset deform read 在形变目录里跑，产出 deformation.h5，再挪到本步目录
    # patch_deform_fix：amset 的签名是 read(bulk_folder, deformation_folders...)，
    # 【未形变的必须排第一个】。原来写成 `read *deform* undeformed`，既顺序
    # 颠倒，又因为 "*deform*" 会匹配到 "undeformed" 自己，把第一个形变目录
    # 当成了 bulk。这个错不报异常，只会安静地给出错误的形变势。
    cmd = ("%s && cd %s && amset deform read undeformed %s "
           ">> deform_read.log 2>&1"
           % (AMSET_ENV_SRC, str(dfm), DEFORM_GLOB))
    print("[..] amset deform read ...")
    rc = subprocess.run(["bash", "-lc", cmd]).returncode
    h5 = dfm / "deformation.h5"
    if rc != 0 or not h5.is_file():
        sys.exit("[ERROR] amset deform read 失败，看 %s/deform_read.log" % dfm)
    dst = out / "deformation.h5"

    # ---- patch_dpt_edgefix：用 amset 权威带边定位，写 band_edges.json ----
    # 旧实现（gen_step12_dpt._band_edge_index）拿 step3_uniform（ISYM=2 IBZ 网格）
    # 的 (band,k) 索引直接去索引 deformation.h5（ISYM=0 全 BZ 网格 + amset 按
    # energy_cutoff 截断后的 band 轴），两套网格根本不是一套，索引必然错位；
    # 错位后静默走"全带中位数"兜底，electron/hole 取到同一个背景值。
    # 这里在 h5 生成的同时，用 amset 自己的 get_vbm/get_cbm 定位带边，band 轴
    # 经 get_ibands 映射到 h5 的截断轴，k 点按 frac-coords 匹配 h5 网格，
    # 把每个载流子的带边 E1（D_xx/D_yy 与面内均值）写进 band_edges.json，
    # step8.2_dpt 的 gen_step12_dpt.py 优先读它，不再自己猜索引。
    # 注意：必须在 h5 移入 out 之前跑（amset read 刚在 dfm 里产出 h5）；
    # json 用绝对路径写到 out，读的相对路径都相对于 dfm。
    _be_code = r'''
import json, numpy as np, glob
from amset.deformation.io import load_deformation_potentials, parse_calculation
from amset.electronic_structure.common import get_ibands
from amset.constants import defaults

import sys
import os as _os
from pathlib import Path as _P
# [PATCH-IONRELAX] 共用 ke_common.resolve_strain_pairs（与 gen_step9_deform.py 单一真源）
sys.path.insert(0, str(_P.cwd().resolve().parent))   # 项目根（本步 cwd 是 step7_deform）
import ke_common
outdir = sys.argv[1]

def _as_dict(edge):
    return edge if isinstance(edge, dict) else {
        "energy": edge.energy, "band_index": edge.band_index,
        "kpoint_index": edge.kpoint_index}

dp, kpoints, structure = load_deformation_potentials("deformation.h5")
bulk = parse_calculation("undeformed")
bs = bulk["bandstructure"]
ib = get_ibands(defaults["energy_cutoff"], bs)
ib = {s.name: np.asarray(v) for s, v in ib.items()}
# [PATCH-DPT-R4] band 轴断言：h5 的 band 轴由 amset deform read 当时的
# energy_cutoff 决定，这里用同一 defaults 重建 ibands；两者不一致说明
# 调用参数漂移了（如 read 传了 -e），必须显式失败而不是静默错位。
for _sp, _d in dp.items():
    if _d.shape[0] != len(ib[_sp.name]):
        raise RuntimeError(
            "band 轴与 h5 不一致（h5=%d, ibands[%s]=%d）：检查 deform read 的 "
            "-e/energy_cutoff" % (_d.shape[0], _sp.name, len(ib[_sp.name])))

out = {}
for carrier, edge in (("electron", bs.get_cbm()), ("hole", bs.get_vbm())):
    e = _as_dict(edge)
    ene = e["energy"]
    hits = []
    for spin, bidxs in e["band_index"].items():
        for b_global in bidxs:
            loc = np.where(ib[spin.name] == b_global)[0]
            if len(loc) == 0:
                continue
            b_local = int(loc[0])
            for k_idx in e["kpoint_index"]:
                kf = np.array(bs.kpoints[k_idx].frac_coords)
                # [PATCH-DPT-R3] k 点周期回卷 + 容差：k=0.999 与 -0.001 是
                # 同一点；不回卷会 argmin 到无关 k（与旧 bug 同类失败）。
                dk = kpoints - kf
                dk -= np.round(dk)
                dd = np.linalg.norm(dk, axis=1)
                kh5 = int(np.argmin(dd))
                if dd[kh5] > 1e-4:
                    raise RuntimeError(
                        "带边 k 点 %s 在 h5 网格找不到（最近距离 %.3g）"
                        % (np.round(kf, 4), dd[kh5]))
                t = dp[spin][b_local, kh5]
                hits.append({"spin": spin.name, "band_global": int(b_global),
                             "band_h5": b_local, "k_h5": int(kh5),
                             "k_frac": np.round(kf, 6).tolist(),
                             "E1_xx_eV": round(float(t[0, 0]), 4),
                             "E1_yy_eV": round(float(t[1, 1]), 4),
                             "E1_iso_eV": round(float(abs((t[0, 0] + t[1, 1]) / 2)), 4)})
    if hits:
        # [PATCH-DPT-R2] 简并 spin×band×k 的命中做 |D| 平均（h5 值已非负），
        # 并报告离散度；不再取 hits[0]（依赖 dict 迭代顺序、不可复现）。
        xs = [h["E1_xx_eV"] for h in hits]
        ys = [h["E1_yy_eV"] for h in hits]
        iso = [h["E1_iso_eV"] for h in hits]
        avg = lambda v: round(float(sum(v)) / len(v), 4)
        spread = lambda v: (max(v) - min(v)) if v else 0.0
        out[carrier] = {
            "edge_energy_eV": round(float(ene), 4),
            "n_hits": len(hits),
            "E1_xx_eV": avg(xs), "E1_yy_eV": avg(ys), "E1_iso_eV": avg(iso),
            "spread_xx": round(spread(xs), 4), "spread_yy": round(spread(ys), 4),
            "hits": hits,
        }
        if spread(xs) > 0.5 or spread(ys) > 0.5:
            print("[WARN] %s 带边简并分量离散度大：xx spread=%.3f yy spread=%.3f"
                  % (carrier, spread(xs), spread(ys)))
    else:
        out[carrier] = {"edge_energy_eV": round(float(ene), 4),
                        "n_hits": 0, "hits": []}

# ---- [PATCH-DPT-R6] 真空能级对齐（Qiao 黑磷路线）----
# amset h5 的 DP 是 ⟨|D|⟩（先取模再平均），无符号、且参考是平均芯势——与文献
# Eq.(9) E1=dE_edge/dγ 口径不同。这里从原始数据重算：
#   E1_vac = |(ΔE_edge - ΔE_vac)/γ|      ← 生成端已取 |·|，写出的字段恒非负
# ΔE_vac 取 undeformed 与 deform-N 的 LOCPOT 真空区平面平均势之差（刚性平移）。
# [A2] 应变分量从结构反解（deform-* 的 POSCAR 晶格 vs undeformed），不硬编码编号。
# [A3/A4] k 点回卷+容差、自旋道按 band_index 选，与上方 amset 带边块共用同一套。
# [B1] 真空区取离所有原子最远的分数间隙中心 ±VAC_WINDOW_HALF，检查平坦度。
# [B2] 形变后校验带边未翻转，结果写进 json 的 edge_flip（不只 print）。
# [B5] 仅 2D 执行；3D 是合法跳过。
# ---- 本轮修复 ----
# [C1] 只有两类是合法跳过：LOCPOT 缺失/未生成（OSError）与 3D 无真空（_VacSkip）。
#      其余守卫（应变反解不全、k 点错位、真空不平坦、dim 读不到）一律抛出——
#      静默降级会产出「看起来正常但其实是 amset 芯势口径」的 dpt_result.json，
#      而 5 个材料自动跑时没人会逐个核 provenance 字符串。
# [C3] dim 从项目根的 step1*/workflow_method.txt 读，与 gen_step12_dpt._read_dim
#      同一套约定（本步 cwd 是 step7_deform，不能只 open 相对路径）。
# [C4] LOCPOT 平面平均势 / vasprun 本征值全部缓存，循环不变量提到内层循环外。
# [C7] 真空窗口按周期拼接（slab 居中时最大间隙跨 z=1 边界），并做半宽敏感性扫描。
# [C8] 每个构型用各自目录的 POSCAR 定位真空窗口。

VAC_WINDOW_HALF = 0.25          # 生产窗口：真空间隙中心 ± 间隙宽度×此值
VAC_WINDOW_SCAN = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)   # [C7] 敏感性扫描
VAC_FLAT_TOL_EV = 1e-3          # 真空区平面平均势平坦度上限（std）
_STEP1_CANDS = ("step1_opt", "step1_std_opt",
                "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")


class _VacSkip(Exception):
    """[C1] 合法跳过真空对齐（3D 无真空），与「守卫失败」区分开。"""


_resolved_paths = {}   # 模块级兜底：3D 走 _VacSkip 提前跳过 try 内初始化，结尾 read_paths 仍可引用

try:
    from pymatgen.io.vasp.outputs import Locpot, Vasprun
    from pymatgen.core import Structure
    from pymatgen.analysis.elasticity.strain import Deformation
    import os as _os
    from pathlib import Path as _P

    # [C3] 维度守卫（[B5]）：从项目根的 step1*/workflow_method.txt 读 DIM=，
    # 与 gen_step12_dpt._read_dim 同一套约定（本步 cwd 是 step7_deform）
    _cwd = _P.cwd().resolve().parent              # 项目根
    _dim = None
    for _name in _STEP1_CANDS:
        _mf = _cwd / _name / "workflow_method.txt"
        if not _mf.is_file():
            continue
        for _ln in _mf.read_text(errors="ignore").splitlines():
            if _ln.strip().upper().startswith("DIM="):
                _dim = _ln.split("=", 1)[1].strip().lower()
                break
        if _dim:
            break
    if _dim is None:
        raise RuntimeError("读不到维度：检查项目根下 %s 的 workflow_method.txt"
                           % " / ".join(_STEP1_CANDS))
    if _dim != "2d":
        raise _VacSkip("dim=%s：真空对齐仅 2D 有意义" % _dim)

    def _k_index(kpts, kf, tol=1e-4):
        """[A3] k 点周期回卷 + 容差匹配（与 amset 带边块同款）。"""
        dk = kpts - kf
        dk -= np.round(dk)
        dd = np.linalg.norm(dk, axis=1)
        k = int(np.argmin(dd))
        if dd[k] > tol:
            raise RuntimeError("k 点 %.4s 不在网格（最近 %.3g）" % (kf, dd[k]))
        return k

    # [C4] vasprun 本征值缓存（key=folder），每构型只解析一次
    _vr_cache = {}

    _resolved_paths = {}   # folder -> {filename: "ionrelax"|"rigid"}，写进 json 可查

    def _resolved(folder, filename):
        """[PATCH-IONRELAX] 优先读 folder/ionrelax/filename（离子弛豫构型），
        否则读本级。E1 用弛豫后构型（IBRION=2 弛豫内坐标），amset h5 仍用
        本级刚性单点——两套口径并列，互不影响。实际读取口径记进 _resolved_paths。"""
        p = _os.path.join(folder, "ionrelax", filename)
        if _os.path.isfile(p):
            _resolved_paths.setdefault(folder, {})[filename] = "ionrelax"
            return p
        _resolved_paths.setdefault(folder, {})[filename] = "rigid"
        return _os.path.join(folder, filename)

    def _edge_raw(folder, kf, band, spin):
        """[A3/A4+C4] 读带边 (energy, occupancy)：回卷+容差匹配 k，自旋道显式选。
        
        [C4] 缓存 vasprun 解析结果（spin×band 双重循环里会反复读同一 folder）。"""
        if folder not in _vr_cache:
            v = Vasprun(_resolved(folder, "vasprun.xml"),
                        parse_dos=False, parse_potcar_file=False)
            _vr_cache[folder] = (np.array(v.actual_kpoints), v.eigenvalues)
        kpts, eigmap = _vr_cache[folder]
        k = _k_index(kpts, kf)
        ev = np.asarray(eigmap[spin])       # 自旋道本征值 (nk, nb, 2)
        return float(ev[k, band, 0]), float(ev[k, band, 1])

    def _vac_window(structure, axis=2, half_ratio=VAC_WINDOW_HALF):
        """[B1+C7] 沿真空轴找离所有原子最远的分数间隙，返回中心 ±half_ratio 窗口。
        
        返回契约（统一 4 元组）：
          不跨边界 → (a, b, None, None)，窗口 = 单段 [a, b]
          跨边界   → (a, 1.0, 0.0, b)，窗口 = 两段 [a, 1] + [0, b]
        判据在 _vac_level 里用 win[2] is None 区分（不是 len(win)）。
        回卷间隙（f[0]+1-f[-1]）也参与选最大，所以 slab 居中时能正确取跨 z=1 的真空。"""
        fracs = np.array([s.frac_coords for s in structure])
        f = np.sort(fracs[:, axis] % 1.0)
        gaps = np.diff(f).tolist() + [f[0] + 1.0 - f[-1]]   # n-1 个相邻 + 回卷
        imax = int(np.argmax(gaps))
        lo = f[imax]
        # 回卷间隙被选中时 hi = f[0]+1.0（> f[-1] = lo，故 lo<hi 恒成立）
        hi = f[imax + 1] if imax < len(f) - 1 else f[0] + 1.0
        c = (lo + hi) / 2.0
        w = (hi - lo) * half_ratio
        a, b = c - w, c + w
        if b > 1.0:            # 窗口右端越界，回卷到 [0, b-1]
            b -= 1.0
        if a < 0.0:
            a += 1.0
        if a > b:              # 回卷后左>右 → 两段
            return a, 1.0, 0.0, b
        return a, b, None, None

    # [C4] LOCPOT 平面平均势缓存（key=folder），每构型只解析一次
    _vz_cache = {}
    # [C8] POSCAR 结构缓存（由调用者管理，在 ax 循环里按需读取）
    _st_cache = {}

    def _vac_level(folder, structure, axis=2, half_ratio=VAC_WINDOW_HALF):
        """[B1+C7+C8] LOCPOT 真空区平面平均势，平坦度 std<VAC_FLAT_TOL_EV 否则报错。
        
        [C8] structure 参数必须是 folder 对应的结构（调用者负责读取和缓存）。
        [C7] 窗口可能跨周期边界（lo>1 或 hi<lo），周期拼接后取平均。"""
        if folder not in _vz_cache:
            lp = Locpot.from_file(_resolved(folder, "LOCPOT"))
            _vz_cache[folder] = np.asarray(lp.get_average_along_axis(axis))
        vz = _vz_cache[folder]
        n = len(vz)
        win = _vac_window(structure, axis, half_ratio)
        if win[2] is None:       # 不跨边界 [a, b]（单段）
            a = int(np.clip(np.floor(win[0] * n), 0, n - 1))
            b = int(np.clip(np.ceil(win[1] * n), a + 1, n))
            seg = vz[a:b]
        else:                    # 跨边界 [a, 1] + [0, b]，两段拼接
            a1 = int(np.clip(np.floor(win[0] * n), 0, n - 1))
            b1 = n
            a2 = 0
            b2 = int(np.clip(np.ceil(win[3] * n), 1, n))
            seg = np.concatenate([vz[a1:b1], vz[a2:b2]])
        if float(np.std(seg)) > VAC_FLAT_TOL_EV:
            raise RuntimeError("真空区平面平均势不平坦 std=%.3g eV（可能取到 slab 尾巴）"
                               % np.std(seg))
        return float(np.mean(seg))

    # [A2] 应变配对反解：与 gen_step9_deform.py 共用 ke_common.resolve_strain_pairs
    # （单一真源，避免 gen 建 ionrelax/ 与 step7b 找 ionrelax/ 各写一份反解而错位）
    _pairs, _strain_mag = ke_common.resolve_strain_pairs(".")
    if _pairs is None:
        raise RuntimeError("应变反解不完整：需 ±0.5% 面内形变各一对")

    for carrier, edge in (("electron", bs.get_cbm()), ("hole", bs.get_vbm())):
        e = _as_dict(edge)
        bi = e["band_index"]; ki = e["kpoint_index"]
        kf = np.array(bs.kpoints[ki[0]].frac_coords)
        # [C4] 循环不变量：undeformed 的能级与真空势（与 spin×band 无关）
        e0_cache = {}       # (spin, band) -> (energy, occ)
        for spin, bidxs in bi.items():
            for band in bidxs:
                e0_cache[(spin, band)] = _edge_raw("undeformed", kf, band, spin)
        v0 = _vac_level("undeformed", structure)
        # [C6] 占据态翻转累计（写进 json，不只 print）
        edge_flip = []
        vac = {}
        vac_scan = {}       # [C7] 窗口敏感性扫描：{half_ratio: {ax: E1}}
        for ax, (dp_, dm_) in _pairs.items():
            g = _strain_mag[ax] if _strain_mag[ax] > 1e-6 else 0.005
            # [C8] 读 dp_/dm_ 各自的 POSCAR 并缓存（形变后原子位置不同；
            # ionrelax 存在时用弛豫后结构，真空窗口随离子位置移动）
            if dp_ not in _st_cache:
                _st_cache[dp_] = Structure.from_file(_resolved(dp_, "POSCAR"))
            if dm_ not in _st_cache:
                _st_cache[dm_] = Structure.from_file(_resolved(dm_, "POSCAR"))
            # [C4] 形变构型的真空势提到 spin×band 循环外（只依赖构型，不依赖带）
            v_p = _vac_level(dp_, _st_cache[dp_])
            v_m = _vac_level(dm_, _st_cache[dm_])
            dvac = ((v_p - v0) / g + (v_m - v0) / (-g)) / 2
            # [A4/B2] 遍历所有自旋道×带（简并时平均）；形变后校验该带边占据态未翻转
            vals = []
            for spin, bidxs in bi.items():
                for band in bidxs:
                    e0, o0 = e0_cache[(spin, band)]
                    e_p, o_p = _edge_raw(dp_, kf, band, spin)
                    e_m, o_m = _edge_raw(dm_, kf, band, spin)
                    # [B2+C6] 占据态翻转校验：undeformed 的带边占据态应与形变后一致
                    # （electron 应恒空 o≈0、hole 应恒满 o≈1）；翻转说明带序交换
                    if (o_p - 0.5) * (o0 - 0.5) < 0 or (o_m - 0.5) * (o0 - 0.5) < 0:
                        _msg = "%s %s band=%d spin=%s 形变后占据态翻转" % (
                            ax, carrier, band, spin.name)
                        print("[WARN] %s（带序交换）" % _msg)
                        edge_flip.append(_msg)
                    raw = ((e_p - e0) / g + (e_m - e0) / (-g)) / 2
                    vals.append(abs(raw - dvac))
            vac[ax] = round(float(sum(vals)) / len(vals), 4) if vals else None
            # [C7] 窗口半宽敏感性扫描（只在生产窗口之外试，生产值就是 vac[ax]）
            for _hr in VAC_WINDOW_SCAN:
                if abs(_hr - VAC_WINDOW_HALF) < 1e-9:
                    continue        # 已算过
                try:
                    # [Bug B] v0 也用同一 _hr 重算（否则扫描的参考窗口宽与 _vp/_vm 不一致，
                    # 会污染「窗口是否影响 dE_vac/dγ」的结论）
                    _v0 = _vac_level("undeformed", structure, half_ratio=_hr)
                    _vp = _vac_level(dp_, _st_cache[dp_], half_ratio=_hr)
                    _vm = _vac_level(dm_, _st_cache[dm_], half_ratio=_hr)
                    _dv = ((_vp - _v0) / g + (_vm - _v0) / (-g)) / 2
                    _scan_vals = []
                    for spin, bidxs in bi.items():
                        for band in bidxs:
                            e0, o0 = e0_cache[(spin, band)]
                            e_p, o_p = _edge_raw(dp_, kf, band, spin)
                            e_m, o_m = _edge_raw(dm_, kf, band, spin)
                            _r = ((e_p - e0) / g + (e_m - e0) / (-g)) / 2
                            _scan_vals.append(abs(_r - _dv))
                    if _hr not in vac_scan:
                        vac_scan[_hr] = {}
                    vac_scan[_hr][ax] = (round(float(sum(_scan_vals)) / len(_scan_vals), 4)
                                         if _scan_vals else None)
                except RuntimeError as _se:
                    # [C1] 只吞「该窗口平坦度不过」——这是扫描的正常结果之一；
                    # k 点错位等守卫仍必须炸出来，否则 C1 的漏洞在扫描路径复活
                    if "不平坦" not in str(_se):
                        raise
        if carrier in out and vac.get("xx") is not None and vac.get("yy") is not None:
            out[carrier]["E1_vac_xx_eV"] = vac["xx"]
            out[carrier]["E1_vac_yy_eV"] = vac["yy"]
            out[carrier]["E1_vac_iso_eV"] = round((vac["xx"] + vac["yy"]) / 2, 4)
            if edge_flip:       # [C6]
                out[carrier]["edge_flip"] = edge_flip
            if vac_scan:        # [C7]
                out[carrier]["vac_window_scan"] = {
                    str(_hr): {"xx": _v.get("xx"), "yy": _v.get("yy"),
                               # 用 is not None（0.0 是合法值，不能被 falsy 判掉）
                               "iso": (round((_v["xx"] + _v["yy"]) / 2, 4)
                                       if _v.get("xx") is not None
                                       and _v.get("yy") is not None else None)}
                    for _hr, _v in sorted(vac_scan.items())}
                # 立即计算该 carrier 的窗口扫描评判
                vals = [v.get("iso") for v in out[carrier]["vac_window_scan"].values()
                        if v.get("iso") is not None]
                if len(vals) >= 2:
                    avg = sum(vals) / len(vals)
                    std = (sum((x - avg) ** 2 for x in vals) / len(vals)) ** 0.5
                    cv = std / avg if avg > 1e-9 else 0.0
                    diffs = [vals[i+1] - vals[i] for i in range(len(vals)-1)]
                    sign_flips = sum(1 for i in range(len(diffs)-1)
                                     if diffs[i] * diffs[i+1] < 0)
                    _verdict = {
                        "n_windows": len(vals), "mean_iso_eV": round(avg, 4),
                        "std_eV": round(std, 4), "CV": round(cv, 4),
                        "sign_flips": sign_flips,
                        "recommend": ("窗口依赖弱（CV < 5%），生产值可靠" if cv < 0.05 else
                                      "窗口依赖中等（CV 5-15%），建议扩大扫描或取中位数" if cv < 0.15 else
                                      "窗口依赖强（CV > 15%），真空区可能不平坦/形变应变反解有误")}
                    if sign_flips >= 2:
                        _verdict["recommend"] += "；振荡（%d 次翻转，窗口可能伸进 slab）" % sign_flips
                    out[carrier]["scan_verdict"] = _verdict
                    _rec = _verdict["recommend"]
                else:
                    _verdict = {}
                    _rec = ""
            else:
                _verdict = {}
                _rec = ""
            print("[OK] %s 真空对齐 E1: xx=%.3f yy=%.3f (window_half=%.2f, scan=%d%s)"
                  % (carrier, vac["xx"], vac["yy"], VAC_WINDOW_HALF, len(vac_scan),
                     (" CV=%.1f%% %s" % (_verdict.get("CV", 0)*100, _rec.split("；")[0])
                      if _verdict else "")))
    # [C7] scan_verdict 已在 carrier 循环内计算并写进各 carrier 的字段
    out["vac_align"] = {"status": "ok", "window_half": VAC_WINDOW_HALF,
                        "flat_tol_eV": VAC_FLAT_TOL_EV,
                        "strain_mag": {k: round(v, 6) for k, v in _strain_mag.items()},
                        "pairs": {k: list(v) for k, v in _pairs.items()}}
# [C1] 只有这两类是合法跳过；其余守卫失败必须炸出来（静默降级 = R5 要根除的失败模式）
except _VacSkip as _e:
    print("[SKIP] %s" % _e)
    out["vac_align"] = {"status": "skipped", "reason": str(_e)}
except (FileNotFoundError, OSError) as _e:
    # LOCPOT/POSCAR 没生成（LVHAR 未开、单点没跑完）——合法跳过，consumer 端会硬报错
    print("[WARN] 真空对齐跳过（%s: %s）：LOCPOT 缺失或 LVHAR 未开" % (
        type(_e).__name__, _e))
    out["vac_align"] = {"status": "skipped",
                        "reason": "%s: %s" % (type(_e).__name__, _e)}
except ImportError as _e:
    print("[WARN] 真空对齐跳过（缺依赖 %s）" % _e)
    out["vac_align"] = {"status": "skipped", "reason": "ImportError: %s" % _e}
# 注意：这里【故意】不接 RuntimeError / 其它 Exception ——
#   应变反解不完整、k 点不在网格、真空区不平坦、维度读不到，
#   全部是「结果不可信」而不是「本体系不适用」，必须让本步 FAIL、
#   让 tf 判 error，而不是写出一份少了 E1_vac_* 字段的 band_edges.json。

# [SKILL_REV] 版本戳 + 实际读取路径（ionrelax 还是 rigid），consumer 交叉校验
out["skill_rev"] = sys.argv[2] if len(sys.argv) > 2 else "UNKNOWN"
out["read_paths"] = {k: dict(v) for k, v in _resolved_paths.items()}

with open(outdir + "/band_edges.json", "w") as fh:
    json.dump(out, fh, indent=2, ensure_ascii=False)
print("[OK] band_edges.json 已生成（amset 权威带边 E1 + 真空对齐 E1_vac）")
'''
    be_out = out / "band_edges.json"
    be_cmd = ("%s && cd %s && python3 -c %s %s %s > band_edges.log 2>&1"
              % (AMSET_ENV_SRC, shlex.quote(str(dfm)),
                 shlex.quote(_be_code), shlex.quote(str(out)),
                 shlex.quote(_SKILL_REV)))
    _rc = subprocess.run(["bash", "-lc", be_cmd]).returncode
    if _rc != 0 or not be_out.is_file():
        # [PATCH-DPT-R5] 硬失败：band_edges.json 是 step8.2_dpt 正确 E1 的唯一
        # 来源，缺失时回退旧逻辑 = 回退「全带中位数」bug。宁可显式报错。
        _tail = ""
        _log = dfm / "band_edges.log"
        if _log.is_file():
            _tail = _log.read_text(errors="ignore")[-400:]
        sys.exit("[ERROR] band_edges.json 生成失败（step8.2_dpt 将无法取到带边 E1，"
                 "请勿回退旧逻辑）。看 %s/band_edges.log：\n%s"
                 % (dfm, _tail))
    print("[DONE] %s：band_edges.json 已生成" % OUTDIR_NAME)

    if dst.exists():
        dst.unlink()
    os.replace(str(h5), str(dst))
    print("[DONE] %s：deformation.h5 已生成" % OUTDIR_NAME)


if __name__ == "__main__":
    main()
