#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_static.py —— 原胞静态自洽（step2_static）。

从 step1 弛豫结果接力，做一次静态 SCF：产出带隙（判金属，决定要不要 NAC）、
作声子参考胞。参数走 step.conf（本步 [params] + 全局 FUNC）。
产出目录：step2_static/
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
from dim_common import require_dim  # noqa: E402
import stepconf

OUTDIR = "step2_static"
STEP   = "step2_static"
PREV   = ["step1_std_opt"]

# step.conf [params] 本脚本认识的键 -> (默认, 类型)
SPEC = {
    "FUNC":      ("pbesol", "str"),   # 与 step1 一致；auto 时取继承值
    "KSPACING":  ("0.03",   "str"),   # 静态密网格
    "KSCHEME":   ("2",      "str"),
    "ENCUT":     (None,     "int"),   # None=从 POTCAR 自动
    "ENCUT_FACTOR": (1.5,   "float"),
    "VASPKIT_EXE": ("vaspkit", "str"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_std_opt 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_std_opt")

    meth = kc.read_method(prev / kc.METHOD_FILE)
    dim = (meth.get("DIM", "").lower() or kc.resolve_dim(out / "POSCAR")[0])
    _, vac_axis = kc.resolve_dim(out / "POSCAR", dim)
    require_dim(dim, ('2d', '3d'), "step2_static",
                why="静态自洽本身对分子成立，但本脚本的 KPOINTS 仍按固体网格生成；要跑 0D 请先照 band-dft-cpu 的 gen_step2_static 补 Gamma 分支")
    func = conf["FUNC"]
    if func in (None, "", "auto"):
        func = meth.get("FUNC", "pbesol").lower()
    print("[..] 维度=%s  泛函=%s（继承 step1）" % (dim.upper(), func))

    kc.vaspkit_kpoints(out, conf["KSCHEME"], conf["KSPACING"],
                       conf["VASPKIT_EXE"], dim, vac_axis)
    kc.vaspkit_potcar(out, conf["VASPKIT_EXE"])
    encut = conf["ENCUT"] or kc.encut_from_potcar(out / "POTCAR", conf["ENCUT_FACTOR"])

    here = Path(__file__).resolve().parent
    incar_tpl = kc.resolve_submit(here, dim, "incar")  # incar_<dim>.tpl
    subs = {"SYSTEM": "%s static" % cwd.name, "ENCUT": encut,
            "GGA": kc.GGA_MAP.get(func, "PS"),
            "VDW_LINE": ("IVDW = %s" % kc.VDW_MAP[func]) if kc.VDW_MAP.get(func) else "# no vdW"}
    kc.render_tpl(incar_tpl, subs, out / "INCAR")

    submit_tpl = kc.resolve_submit(here, dim, "submit_std")
    kc.write_submit(submit_tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S2static")})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：静态自洽输入就绪" % OUTDIR)


if __name__ == "__main__":
    main()
