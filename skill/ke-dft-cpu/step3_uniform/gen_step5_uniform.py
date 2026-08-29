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
KSPACING     = "0.03"                 # ★AMSET 密网格；要更密改这里
KMIN_DIV     = 8                      # 每面内轴最小分割数（全局 KSPACING 缩放保证笛卡尔准均匀；8 已够 ≥2 壳层）
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
        dim, vac_axis = kc.resolve_dim_for(out / "POSCAR", DIMENSION)
    else:
        _, vac_axis = kc.resolve_dim_for(out / "POSCAR", dim)
        require_dim(dim, ('2d', '3d'), "step3_uniform",
                    why="载流子输运/形变势建立在能带色散上，孤立分子没有色散")
    print("[..] 维度：%s" % dim.upper())
    kc.write_method(out / kc.METHOD_FILE, dim, "uniform 密网格自洽",
                    func=_func)

    kc.vaspkit_kpoints(out, KSCHEME, KSPACING, VASPKIT_EXE, dim, vac_axis)
    # [KMIN_DIV] 每面内轴最小分割数。用全局 KSPACING 缩放（不是按轴 max）：
    # vaspkit 的 KSPACING 生成的是笛卡尔准均匀网格，缩放保持这个性质——m* 拟合
    # 需要的是"面内笛卡尔间距可比"（各向异性 ≲2×），不是"每轴分割数够多"。
    # 按轴 max 会破坏它（LS 12×12 的 Δk_x=0.024 vs Δk_y=0.168，6.9× 各向异性，
    # 圆盘里 y 方向无点，轴覆盖守卫直接报错）。全局缩放 LS → 8×44（各向异性 1.3×）。
    if KMIN_DIV and dim == "2d":
        _ks = float(KSPACING)
        for _ in range(4):
            _kpt = (out / "KPOINTS").read_text().splitlines()
            try:
                _nx, _ny, _nz = (int(x) for x in _kpt[3].split())
            except (IndexError, ValueError):
                _nx = _ny = 0
            if min(_nx, _ny) >= KMIN_DIV:
                break
            _ks = _ks * (min(_nx, _ny) / KMIN_DIV)
            print("[WARN] KPOINTS 面内分割 %dx%d < KMIN_DIV=%d，KSPACING -> %.6f"
                  % (_nx, _ny, KMIN_DIV, _ks))
            kc.vaspkit_kpoints(out, KSCHEME, "%.6f" % _ks, VASPKIT_EXE, dim, vac_axis)
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
