#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_mace_static.py —— MACE 静态单点（step2_mace_static），submit 模式。

接力 step1_mace_relax 的 CONTCAR，写 submit.sh 把静态单点放到计算节点作业里跑。
产出（作业跑完后）：static_summary.json（E_tot / E_per_atom，供 step3 算形成能）。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
import stepconf

OUTDIR = "step2_mace_static"
STEP = "step2_mace_static"
PREV = ["step1_mace_relax"]

SPEC = {
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("cpu", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    # ---- 跨步骤键：step1/step3 的参数经三层合并会带进本步，声明但不消费 ----
    "DIMENSION": ("auto", "str"), "RELAX": (True, "bool"),
    "RELAX_CELL": (True, "bool"), "FMAX": (1e-4, "float"),
    "MAX_STEPS": (2000, "int"), "OPTIMIZER": ("FIRE", "str"),
    "FIX_SYMMETRY": (True, "bool"), "SYMPREC": (1e-4, "float"),
    "CELL_POLICY": ("primitive", "str"), "RESIDUAL_TOL": (2e-3, "float"),
    "STRESS_TOL": (0.05, "float"),
    "MU": (None, "elemmap"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_mace_relax 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_mace_relax")
    for f in (kc.METHOD_FILE,):
        if (prev / f).is_file():
            shutil.copyfile(str(prev / f), str(out / f))

    here = Path(__file__).resolve().parent
    for f in ("mace_static.py", "mace_model.py"):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    cmd = ("python mace_static.py "
           "--model %s --model-dir '%s' --device %s --dtype %s"
           % (conf["MACE_MODEL"], conf["MACE_MODEL_DIR"] or "",
              conf["DEVICE"] or "cpu", conf["DTYPE"] or "float64"))

    tpl = kc.resolve_submit(here, "submit_mace_opt")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S2static", tag="opt"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": cmd,
                     "LOG": "mace_static.log"})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 static_summary.json）" % OUTDIR)


if __name__ == "__main__":
    main()
