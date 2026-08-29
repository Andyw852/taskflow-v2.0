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
DK_MAX       = 0.05                   # 面内笛卡尔 k 间距上限 (Å⁻¹)：N_i=ceil(|b_i|/DK_MAX)，
                                      # 逐轴 max(vaspkit, N_i)。笛卡尔量做下限，细长胞不会失真
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
    # [DK_MAX] 面内笛卡尔 k 间距上限。判据是笛卡尔间距（不是分割数下限）——
    # 分割数下限对不同长度的胞给完全不同的笛卡尔间距（SS a=10.82 vs LS a=21.64，
    # 同样 N_x=8 给 0.0726 vs 0.0363），m* 拟合半径和收敛性都失真。逐轴
    # max(vaspkit, ceil(|b_i|/DK_MAX)) 是安全的：下限本身是笛卡尔量，不会造出
    # 6.9× 各向异性（SS→12×41，LS→6×41，CrS2 15×15 不变）。
    if DK_MAX and dim == "2d":
        import numpy as np
        _ln = (out / "POSCAR").read_text().splitlines()
        _s = float(_ln[1].split()[0])
        _a = np.array([float(x) for x in _ln[2].split()[:3]]) * _s
        _b = np.array([float(x) for x in _ln[3].split()[:3]]) * _s
        _c = np.array([float(x) for x in _ln[4].split()[:3]]) * _s
        _vol = abs(float(np.dot(_a, np.cross(_b, _c))))
        _b1 = 2.0 * np.pi * np.cross(_b, _c) / _vol
        _b2 = 2.0 * np.pi * np.cross(_c, _a) / _vol
        _len = [float(np.linalg.norm(_b1)), float(np.linalg.norm(_b2))]
        # [DK_MAX 豁免] 面内倒格矢各向同性（|b1|≈|b2|，六方/正方）→ 带边在高对称
        # 点（K/Γ），对称性帮邻域补齐，vaspkit 默认网格（15×15 间距 0.15）已够，
        # 不需要 DK_MAX 加密（CrS2/CrSe2 已验证 15×15 给 τ 逐位命中文献）。只有
        # 各向异性胞（正交细长 SS/LS，|b1|≫|b2|，带边在 Y-Γ 非高对称）才强制。
        _ratio = (max(_len) / min(_len)) if min(_len) > 0 else 1.0
        if _ratio < 1.5:
            print("[..] 面内倒格矢各向同性（|b1|/|b2|=%.2f < 1.5），带边在高对称点，"
                  "默认网格已够，跳过 DK_MAX" % _ratio)
            _need = None
        else:
            _need = [int(np.ceil(_len[i] / float(DK_MAX))) for i in (0, 1)]
        _kpt = (out / "KPOINTS").read_text().splitlines()
        try:
            _nx, _ny, _nz = (int(x) for x in _kpt[3].split())
        except (IndexError, ValueError):
            _nx = _ny = _nz = 1
        if _need is None:
            _need = [_nx, _ny]
        _mx, _my = max(_nx, _need[0]), max(_ny, _need[1])
        if (_mx, _my) != (_nx, _ny):
            print("[WARN] 面内分割 %dx%d 间距 %.3f/%.3f > DK_MAX=%.3f，按轴提到 %dx%d"
                  % (_nx, _ny, _len[0] / _nx, _len[1] / _ny, float(DK_MAX), _mx, _my))
            _kpt[3] = "  %d  %d  %d" % (_mx, _my, _nz)
            (out / "KPOINTS").write_text(
                "\n".join(_kpt) + "\n", encoding="utf-8", newline="\n")
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
