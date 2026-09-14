#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step5_dielect.py —— DFPT 介电常数（step5_dielect）。

结构从优化结果接力，IBRION=8 + LEPSILON 一次微扰求 ε∞ 与 ε₀。
产出目录：step5_dielect/，判据看 OUTCAR 的 MACROSCOPIC STATIC DIELECTRIC TENSOR。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ke_common as kc
import stepconf  # noqa: E402
from dim_common import require_dim, resolve_tpl  # noqa: E402

# =========================== 可改参数区 ===========================
# ---------- 体系判别阻断（step2.15_discriminant）----------
# False = 默认拦截 SEMIMETAL/METAL@PBE（这套半导体框架不适用）。
# True  = 强制继续（金属体系也有人要算输运，或 PBE 误判而杂化还没来得及重判）。
# ★ 不是硬停：读不到 discriminant.json 时一律放行，行为与加这道闸门前完全一致。
FORCE_TRANSPORT = False


def _disc_gate():
    """体系判别闸门：默认拦截 SEMIMETAL/METAL@PBE，FORCE_TRANSPORT 可覆盖。"""
    import sys as _s
    from pathlib import Path as _P
    try:
        _s.path.insert(0, str(_P(__file__).resolve().parent))
        import discriminant_common as _dc
        if not _dc.gate(_P.cwd(), "step5_dielect", force=FORCE_TRANSPORT):
            _s.exit("[BLOCKED] 体系判别为 SEMIMETAL/METAL@PBE —— step5_dielect 已阻断。"
                    "完整提示见 step2_bandgap/step2.15_discriminant/discriminant.json "
                    "的 block.hint；强制继续请把本脚本顶部 FORCE_TRANSPORT 设为 True。")
    except SystemExit:
        raise
    except Exception as _e:
        print("[WARN] 体系判别闸门异常，放行：%s" % _e, file=_s.stderr)


OUTDIR_NAME  = "step5_dielect"
PREV_CANDS   = ["step1_opt", "step1_std_opt"]
DIMENSION    = "auto"
VASPKIT_EXE  = "vaspkit"
KSCHEME      = "2"
KSPACING     = "0.04"          # DFPT 比 uniform 稍稀即可
FUNC         = "inherit"      # patch_ke_dag: inherit=继承 step1
# VASP 的 vdW 修正不进 DFPT 响应（见 wiki IVDW 词条），默认把 D3 剥掉；
# 想强行保留（只影响总能，不影响 ε）把下面改 True。
KEEP_D3_IN_DFPT = False
MANUAL_ENCUT = None
ENCUT_FACTOR = 1.5
STEP_LABEL   = "S5_dielect"
# =================================================================
GGA_MAP = {"pbe": "PE", "pbesol": "PS", "pbe-d3": "PE"}

def main():
    _disc_gate()
    cwd = Path.cwd(); out = cwd / OUTDIR_NAME; out.mkdir(exist_ok=True)
    prev = kc.find_prev_dir(cwd, PREV_CANDS)
    if prev is None:
        sys.exit("[ERROR] 找不到含 CONTCAR 的上一步：%s" % PREV_CANDS)
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_opt")
    _func, _subs = kc.resolve_func(prev, FUNC, OUTDIR_NAME,
                                   drop_d3=not KEEP_D3_IN_DFPT)
    dim = kc.read_method_dim(prev / kc.METHOD_FILE) \
        or kc.resolve_dim_for(out / "POSCAR", DIMENSION)[0]
    _, vac_axis = kc.resolve_dim_for(out / "POSCAR", dim)
    require_dim(dim, ('2d', '3d'), "step5_dielect",
                why="DFPT 给的是介电张量；分子对应的是极化率，定义和量纲都不同")
    print("[..] 维度：%s" % dim.upper())
    kc.write_method(out / kc.METHOD_FILE, dim, "DFPT 介电常数",
                    func=_func)
    kc.vaspkit_kpoints(out, KSCHEME, KSPACING, VASPKIT_EXE, dim, vac_axis)
    kc.vaspkit_potcar(out, VASPKIT_EXE)
    encut = MANUAL_ENCUT or kc.encut_from_potcar(out / "POTCAR", ENCUT_FACTOR)
    tpl = Path(__file__).resolve().parent / ("incar_dfpt_%s.tpl" % dim)
    if not tpl.is_file():
        sys.exit("[ERROR] 找不到模板 %s" % tpl.name)
    _sub = {"SYSTEM": cwd.name + " DFPT", "ENCUT": encut}
    _sub.update(_subs)
    kc.render_tpl(tpl, _sub, out / "INCAR")
    # DFPT(IBRION=8) 在多数 VASP 版本不支持 LDA+U，故 with_u=False
    kc.inherit_scf_tags(out / "INCAR", cwd, with_u=False, label="dielect")
    submit_tpl = resolve_tpl(Path(__file__).resolve().parent, "submit_std", dim)
    submit = out / "submit.sh"
    submit.write_text(submit_tpl.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    kc.patch_submit_jobname(submit, kc.new_jobname(cwd, STEP_LABEL))
    stepconf.apply_submit(submit, stepconf.read_submit(stepconf.CONF_NAME))
    print("[DONE] %s：DFPT 输入就绪（KPAR=NCORE=1），可提交" % OUTDIR_NAME)

if __name__ == "__main__":
    main()
