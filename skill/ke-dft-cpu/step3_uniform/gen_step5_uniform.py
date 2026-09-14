#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_uniform.py —— AMSET uniform 密网格自洽（step3_uniform）。

在材料目录下运行，从结构优化结果接力：
  1. POSCAR ← step1_std_opt/CONTCAR
  2. VASPKIT 生成密 KPOINTS（kspacing 见下）+ POTCAR
  3. 按 2D/3D 渲染 incar_uniform_*.tpl，产出 WAVECAR 供 amset wave
产出目录：step3_uniform/
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ke_common as kc
import stepconf  # noqa: E402
from dim_common import require_dim, resolve_tpl  # noqa: E402

# =========================== 可改参数区 ===========================
OUTDIR_NAME  = "step3_uniform"
PREV_CANDS   = ["step1_opt", "step1_std_opt"]      # 结构来源（找第一个有 CONTCAR 的）
DIMENSION    = "auto"                 # auto | 2d | 3d
VASPKIT_EXE  = "vaspkit"
KSCHEME      = "2"                    # 2 = Γ 心
KSPACING     = "0.03"                 # vaspkit 起点（与静态同值，所以它【不能】单独
                                      # 保证加密 —— 真正定密度的是下面的 DK_MAX）
# ---- 笛卡尔 k 间距上限（本步"密网格"的主判据）----------------------------
#   N_i = max(vaspkit, ceil(|b_i| / DK_MAX))，逐轴。判据必须是笛卡尔的：
#   能带曲率在笛卡尔 k 空间有绝对尺度，与胞长短无关（分割数下限做不到这点）。
#   ★值按维度【分档】，而不是按维度【开关】：
#     点数在 2D ∝ N²、3D ∝ N³，三维成本紧一档，所以 3D 放宽到 0.08。
#     （历史坑：原来是  if DK_MAX and dim == "2d"  —— 3D 整段跳过，于是 uniform
#      退化成 vaspkit(KSPACING=0.03)，而静态也用 0.03 → 两网格逐轴相同，
#      "密网格"步骤实际给的是静态网格。扫 10 个走这条链的项目，8 个中招，
#      含 Si / Si_diamond / Mg2C60 / Mg4C60 / Mo2S3 / AlN / MoS2。）
#   DK_MAX = None  → 按维度自动取 DK_MAX_2D / DK_MAX_3D
#   DK_MAX = 数值  → 强制覆盖（半金属/小带隙体系建议显式收紧到 0.05）
DK_MAX       = None
DK_MAX_2D    = "0.05"
DK_MAX_3D    = "0.06"
# ---- 成本护栏：网格总点数上限 -------------------------------------------
#   超了就【报错要求显式覆盖】，而不是静默降密 —— 否则将来又有人用"跳过加密"
#   绕过，回到同一个坑。参考量级：Si(5.43Å) 0.08 → 25³ ≈ 1.6e4；
#   Mg4C60 0.05 → 13×13×8 ≈ 1.4e3。
UNIFORM_NMAX = 20000
FUNC         = "inherit"              # patch_ke_dag: inherit=继承 step1
                                      # 也可写死 pbe | pbesol | pbe-d3
MANUAL_ENCUT = None                   # None=从 POTCAR 自动；或写数值
ENCUT_FACTOR = 1.5
STEP_LABEL   = "S3_uniform"
# =================================================================

GGA_MAP = {"pbe": "PE", "pbesol": "PS", "pbe-d3": "PE"}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)

    prev = kc.find_prev_dir(cwd, PREV_CANDS)
    if prev is None:
        sys.exit("[ERROR] 找不到含 CONTCAR 的上一步目录：%s" % PREV_CANDS)
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_opt")
    _func, _subs = kc.resolve_func(prev, FUNC, OUTDIR_NAME)

    dim = kc.read_method_dim(prev / kc.METHOD_FILE)
    if dim is None:
        # ★ 走到这里说明 dim 是【猜】出来的，不是从 workflow_method.txt 读的。
        #   层状体相（如 225 相 A2B2Te5，c≈17 Å）容易被真空判据误判成 2D，
        #   一旦判成 2D，下面会把真空方向 kz 压成 1 —— 对体相是致命的
        #   （面外 σ/S/κ_e 全废）。所以必须响亮提示，并要求人工确认。
        print("[WARN] 读不到 %s（step1 目录可能已清理）—— dim 将由结构自动判定为 %s。"
              % (kc.METHOD_FILE, str(DIMENSION)))
        print("[WARN] 层状【体相】极易被误判成 2D（会把 kz 压成 1，面外输运全废）；"
              "请人工确认，或用顶部 DIMENSION = \"3d\" 强制。")
        dim, vac_axis = kc.resolve_dim_for(out / "POSCAR", DIMENSION)
        print("[WARN] 自动判定结果：dim=%s（如不对，请设 DIMENSION 后重新 gen）" % dim.upper())
    else:
        _, vac_axis = kc.resolve_dim_for(out / "POSCAR", dim)
        require_dim(dim, ('2d', '3d'), "step3_uniform",
                    why="载流子输运/形变势建立在能带色散上，孤立分子没有色散")
    print("[..] 维度：%s" % dim.upper())
    kc.write_method(out / kc.METHOD_FILE, dim, "uniform 密网格自洽",
                    func=_func)

    kc.vaspkit_kpoints(out, KSCHEME, KSPACING, VASPKIT_EXE, dim, vac_axis)
    # [DK_MAX] 面内笛卡尔 k 间距上限。判据是笛卡尔间距（不是分割数下限）——
    # 分割数下限对不同长度的胞给完全不同的笛卡尔间距（SS a=10.82 vs LS a=21.64，
    # 同样 N_x=8 给 0.0726 vs 0.0363），m* 拟合半径和收敛性都失真。逐轴
    # max(vaspkit, ceil(|b_i|/DK_MAX)) 是安全的：下限本身是笛卡尔量，不会造出
    # 6.9× 各向异性（SS→12×41，LS→6×41，CrS2 15×15 不变）。
    # ------------------------------------------------------------------
    # DK_MAX：对所有【周期性方向】生效（不再按 dim=="2d" 开关）。
    #   三层：① 绝对判据（主，笛卡尔间距按维度分档）
    #         ② 相对下限（兜底，≥2× 静态网格）
    #         ③ 断言 + 成本护栏（不许静默退化）
    # ------------------------------------------------------------------
    import numpy as np
    _ln = (out / "POSCAR").read_text().splitlines()
    _s = float(_ln[1].split()[0])
    _a = np.array([float(x) for x in _ln[2].split()[:3]]) * _s
    _b = np.array([float(x) for x in _ln[3].split()[:3]]) * _s
    _c = np.array([float(x) for x in _ln[4].split()[:3]]) * _s
    _vol = abs(float(np.dot(_a, np.cross(_b, _c))))
    _rec = [2.0 * np.pi * np.cross(_b, _c) / _vol,
            2.0 * np.pi * np.cross(_c, _a) / _vol,
            2.0 * np.pi * np.cross(_a, _b) / _vol]
    _len = [float(np.linalg.norm(v)) for v in _rec]
    _dk = float(DK_MAX) if DK_MAX else float(DK_MAX_2D if dim == "2d" else DK_MAX_3D)
    _axes = (0, 1) if dim == "2d" else (0, 1, 2)     # 2D 的真空轴已是 kz=1，不动
    _kpt = (out / "KPOINTS").read_text().splitlines()
    try:
        _n = [int(x) for x in _kpt[3].split()]
    except (IndexError, ValueError):
        _n = [1, 1, 1]
    while len(_n) < 3:
        _n.append(1)
    # ① 绝对判据
    _need = list(_n[:3])
    for i in _axes:
        _need[i] = max(_n[i], int(np.ceil(_len[i] / _dk)))
    # ② 相对下限兜底：至少 2× 静态线密度。
    #    注意局限：static 本身欠收敛时，2× 仍然欠收敛 —— 所以这条只是兜底，
    #    真正定密度的必须是 ①。
    _st = None
    for _cand in (cwd / "step2_bandgap" / "step2.1_static" / "KPOINTS",
                  cwd / "step2.1_static" / "KPOINTS"):
        if _cand.is_file():
            try:
                _st = [int(x) for x in _cand.read_text().splitlines()[3].split()][:3]
            except (IndexError, ValueError):
                _st = None
            break
    if _st and len(_st) == 3:
        for i in _axes:
            _need[i] = max(_need[i], 2 * _st[i])
    if _need != _n[:3]:
        print("[WARN] 网格 %dx%dx%d（笛卡尔间距 %.3f/%.3f/%.3f Å⁻¹）不满足 DK_MAX=%.3f"
              "（含 2x 静态下限），按轴提到 %dx%dx%d"
              % (_n[0], _n[1], _n[2], _len[0] / _n[0], _len[1] / _n[1], _len[2] / _n[2],
                 _dk, _need[0], _need[1], _need[2]))
        _kpt[3] = "  %d  %d  %d" % (_need[0], _need[1], _need[2])
        (out / "KPOINTS").write_text(
            "\n".join(_kpt) + "\n", encoding="utf-8", newline="\n")
    # ③a 断言：不许静默退化
    if _st and len(_st) == 3:
        for i in _axes:
            if _need[i] <= _st[i]:
                sys.exit("[ERROR] step3_uniform 网格退化：第 %d 轴 N_uniform=%d ≤ N_static=%d。"
                         "\n        'uniform 密网格' 必须比静态更密。请调小 DK_MAX_2D/DK_MAX_3D、"
                         "显式设 DK_MAX，或检查静态网格是否过粗。" % (i + 1, _need[i], _st[i]))
    # ③b 成本护栏：超预算就报错要求显式覆盖，不静默降密
    _tot = _need[0] * _need[1] * _need[2]
    if _tot > int(UNIFORM_NMAX):
        sys.exit("[ERROR] step3_uniform 网格 %dx%dx%d = %d 点，超过 UNIFORM_NMAX=%s。"
                 "\n        这是成本护栏：请显式把 UNIFORM_NMAX 调到你确认可承受的值，"
                 "或在项目里覆盖 DK_MAX —— 不要用'跳过加密'绕过。"
                 % (_need[0], _need[1], _need[2], _tot, UNIFORM_NMAX))
    print("[OK] 密网格 %dx%dx%d（%d 点，IBZ 会按对称性约化；DK_MAX=%.3f）"
          % (_need[0], _need[1], _need[2], _tot, _dk))
    kc.vaspkit_potcar(out, VASPKIT_EXE)

    encut = MANUAL_ENCUT or kc.encut_from_potcar(out / "POTCAR", ENCUT_FACTOR)
    tpl = Path(__file__).resolve().parent / ("incar_uniform_%s.tpl" % dim)
    if not tpl.is_file():
        sys.exit("[ERROR] 找不到模板 %s" % tpl.name)
    system = cwd.name + " uniform"
    _sub = {"SYSTEM": system, "ENCUT": encut}
    _sub.update(_subs)
    kc.render_tpl(tpl, _sub, out / "INCAR")
    kc.inherit_scf_tags(out / "INCAR", cwd, with_u=True, label="uniform")
    # 并行参数按宿主机自适应（GPU 版强制 NCORE=1/KPAR=1，CPU 保持模板默认）
    kc.apply_parallel_tags(out / "INCAR")

    submit_tpl = resolve_tpl(Path(__file__).resolve().parent, "submit_std", dim)
    submit = out / "submit.sh"
    submit.write_text(submit_tpl.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    kc.patch_submit_jobname(submit, kc.new_jobname(cwd, STEP_LABEL))
    stepconf.apply_submit(submit, stepconf.read_submit(stepconf.CONF_NAME))

    print("[DONE] %s：INCAR/KPOINTS/POTCAR/POSCAR 就绪，可提交" % OUTDIR_NAME)


if __name__ == "__main__":
    main()
